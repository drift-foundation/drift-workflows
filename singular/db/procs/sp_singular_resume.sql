DELIMITER $$
-- Resume EXISTING work. Never inserts. PR1 scope: report ACTIVE (a WORKING lease, even if
-- expired -- expired-lease reclaim is PR2), TERMINAL (DONE/FAILED replay), or NOT_FOUND (no row --
-- a protocol violation: the caller should have start()ed).
--
-- PR1 resume is `resume(key)` ONLY: it grants nothing (reclaim -> PR2, deferred-resume -> PR3), so
-- it takes no lease token / lease_meta / timeout / max-recovery. Those PR2-only inputs were dropped
-- because, when accepted-but-unused, a malformed proposed token could reject/block a legitimate
-- terminal replay. PR2 reintroduces them as a structured recovery request.
-- Result: ONE discriminated JSON document (column `result`) — an OBJECT keyed by `outcome`:
--   {"outcome":"not_found"}
--   {"outcome":"active","lease_expires_at":"..."}
--   {"outcome":"terminal","state":"done|failed","payload":{<authoritative payload document>}}
-- Arm-inapplicable fields are OMITTED, never SQL/JSON null. The payload is the worker's stored
-- result document, embedded as a NESTED JSON object (document semantics, not a JSON-in-a-string).
CREATE PROCEDURE `sp_singular_resume`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32)
)
proc:BEGIN
	DECLARE v_current_event_ts datetime(6);
	DECLARE v_status tinyint unsigned;
	DECLARE v_lease_expires datetime(6);
	DECLARE v_payload mediumtext;
	DECLARE v_head_missing tinyint(1) DEFAULT 0;

	DECLARE CONST_STATUS_WORKING tinyint unsigned DEFAULT 1;
	DECLARE CONST_STATUS_DONE tinyint unsigned DEFAULT 2;
	DECLARE CONST_STATUS_FAILED tinyint unsigned DEFAULT 3;

	-- PR1 resume is READ-ONLY (grants nothing), so it does NOT lock the projection — consistent with
	-- inspect. The FOR UPDATE returns when PR2 resume can actually reclaim/grant a lease.
	SET v_current_event_ts = NULL;
	SELECT `current_event_ts`
	INTO v_current_event_ts
	FROM `tb_singular_work_item`
	WHERE `service_group` = arg_service_group
		AND `idempotency_key` = arg_idempotency_key;

	IF v_current_event_ts IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- Referenced head-history row: explicit presence check (dangling pointer = corruption).
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_head_missing = 1;
		SELECT
			`status`,
			`lease_expires_at`,
			`event_payload`
		INTO
			v_status,
			v_lease_expires,
			v_payload
		FROM `tb_singular_work_item_history`
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key
			AND `event_ts` = v_current_event_ts;
	END;
	IF v_head_missing = 1 THEN
		SIGNAL SQLSTATE '45001' SET MESSAGE_TEXT = 'SingularHeadHistoryMissing', MYSQL_ERRNO = 30001;
	END IF;

	IF v_status = CONST_STATUS_WORKING THEN
		-- PR1: ANY working lease (even expired) -> Active. Reclaim is PR2.
		SELECT JSON_OBJECT(
			'outcome', 'active',
			'lease_expires_at', v_lease_expires
		) AS result;
	ELSEIF v_status = CONST_STATUS_DONE OR v_status = CONST_STATUS_FAILED THEN
		SELECT JSON_OBJECT(
			'outcome', 'terminal',
			'state', IF(v_status = CONST_STATUS_DONE, 'done', 'failed'),
			'payload', JSON_EXTRACT(v_payload, '$')   -- nested object document, not a JSON string
		) AS result;
	ELSE
		-- DEFERRED/INDETERMINATE not reachable in PR1.
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidState';
	END IF;
END $$
DELIMITER ;
