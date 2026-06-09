DELIMITER $$
-- Read the current state of a work item. Reads the PROJECTION first so the two "no data"
-- cases are NOT conflated: a missing projection row is a legitimate NotFound; a projection
-- whose referenced head-history row is absent is BACKEND CORRUPTION (a dangling
-- current_event_ts pointer) and is surfaced as a SIGNAL, never inferred through NULL.
-- Result: ONE discriminated JSON document (column `result`), an OBJECT keyed by `outcome`:
--   {"outcome":"not_found"}
--   {"outcome":"working","lease_owner":"<hex16>","lease_expires_at":"...","checkpoint":{<doc>}}
--   {"outcome":"terminal","state":"done|failed","payload":{<doc>},"checkpoint":{<doc>}}
-- Arm-inapplicable fields are OMITTED. lease_owner is lowercase hex of the 16-byte owner;
-- payload/checkpoint are NESTED JSON objects (document semantics, not JSON-in-a-string).
CREATE PROCEDURE `sp_singular_inspect`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32)
)
proc:BEGIN
	DECLARE v_current_event_ts datetime(6);
	DECLARE v_status tinyint unsigned DEFAULT NULL;
	DECLARE v_event_payload mediumtext;
	DECLARE v_lease_owner binary(16);
	DECLARE v_lease_expires datetime(6);
	DECLARE v_checkpoint mediumtext;
	DECLARE v_head_missing tinyint(1) DEFAULT 0;

	DECLARE CONST_STATUS_WORKING tinyint unsigned DEFAULT 1;
	DECLARE CONST_STATUS_DONE tinyint unsigned DEFAULT 2;
	DECLARE CONST_STATUS_FAILED tinyint unsigned DEFAULT 3;

	-- 1) Projection. Absent → NotFound ({"outcome":"not_found"}, not a SIGNAL).
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

	-- 2) Referenced head-history row. Explicit presence check (NOT FOUND handler + flag) so an
	--    absent row is reported as corruption rather than silently read as NULL.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_head_missing = 1;
		SELECT
			`status`, `event_payload`, `lease_owner`, `lease_expires_at`, `checkpoint_payload`
		INTO
			v_status, v_event_payload, v_lease_owner, v_lease_expires, v_checkpoint
		FROM `tb_singular_work_item_history`
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key
			AND `event_ts` = v_current_event_ts;
	END;
	IF v_head_missing = 1 THEN
		-- Corruption: projection points at a non-existent head event. SQLSTATE 45001 = the
		-- "backend response invalid" class the gateway maps to BackendResponseInvalid.
		SIGNAL SQLSTATE '45001' SET MESSAGE_TEXT = 'SingularHeadHistoryMissing', MYSQL_ERRNO = 30001;
	END IF;

	-- State-specific document: WORKING carries the live-lease snapshot (descriptive owner as
	-- lowercase hex, expiry, checkpoint); a terminal head carries the authoritative result +
	-- the terminal checkpoint. event_payload is `{}` for a WORKING head and is not emitted there.
	IF v_status = CONST_STATUS_WORKING THEN
		SELECT JSON_OBJECT(
			'outcome', 'working',
			'lease_owner', LOWER(HEX(v_lease_owner)),
			'lease_expires_at', v_lease_expires,
			'checkpoint', JSON_EXTRACT(v_checkpoint, '$')   -- nested object document
		) AS result;
	ELSEIF v_status = CONST_STATUS_DONE OR v_status = CONST_STATUS_FAILED THEN
		SELECT JSON_OBJECT(
			'outcome', 'terminal',
			'state', IF(v_status = CONST_STATUS_DONE, 'done', 'failed'),
			'payload', JSON_EXTRACT(v_event_payload, '$'),  -- nested object document
			'checkpoint', JSON_EXTRACT(v_checkpoint, '$')
		) AS result;
	ELSE
		-- DEFERRED/INDETERMINATE not reachable in PR1.
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidState';
	END IF;
END$$
DELIMITER ;
