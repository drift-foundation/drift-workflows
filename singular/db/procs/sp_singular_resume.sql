DELIMITER $$
-- Resume / RECLAIM existing work (PR2). A READ-ONLY resume reports ACTIVE / TERMINAL / NOT_FOUND exactly as
-- PR1. A RECLAIM is requested by supplying a fresh recovery lease (lease_owner + lease_meta + event_ts +
-- absolute lease_expires_at + caller-minted lease_token): on an EXPIRED working item it GRANTS a fresh
-- attempt, ROTATING the stored token (fencing out the prior holder, §4) and handing over the persisted
-- checkpoint context (§6.6). A LIVE (unexpired) working lease is NEVER stolen -> ACTIVE. Terminal items
-- replay; the recovery lease is IGNORED there and its token is NOT validated, so a malformed proposed token
-- can never block a legitimate terminal replay (the reason PR1 dropped these inputs; PR2 reintroduces them
-- as a structured recovery request validated only on the reclaim-eligible path).
--
-- PR2 resume LOCKS the projection (FOR UPDATE) because it may now mutate (the reclaim grant). Expiry is
-- judged against the caller's `event_ts` (the gateway sources backend-now; 0.5 absolute-time contract):
-- expired iff event_ts > stored lease_expires_at. Reclaim is a CLAIMED event (event_type=10) carrying the
-- new lease + the carried-forward checkpoint; `recovery_attempt` is the count of reclaims (derived from the
-- CLAIMED-event history: start is CLAIMED #1, so the first reclaim is attempt 1).
--
-- Result: ONE discriminated JSON document (column `result`), an OBJECT keyed by `outcome`:
--   {"outcome":"not_found"}
--   {"outcome":"active","lease_expires_at":"..."}
--   {"outcome":"terminal","state":"done|failed","payload":{<authoritative payload document>}}
--   {"outcome":"granted","kind":"reclaim","lease_expires_at":"...","recovery_attempt":N,"checkpoint":{...}}
-- Arm-inapplicable fields are OMITTED, never SQL/JSON null. Payload/checkpoint are NESTED JSON objects
-- (document semantics). Reclaim params are NULLABLE: a NULL token => read-only resume; a non-NULL token =>
-- the other recovery inputs are REQUIRED and validated.
CREATE PROCEDURE `sp_singular_resume`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32),
	IN arg_lease_owner varbinary(16),       -- reclaim: new attempt owner (NULL for read-only resume)
	IN arg_lease_meta mediumtext,           -- reclaim: new lease meta OBJECT (NULL for read-only)
	IN arg_event_ts datetime(6),            -- reclaim: claim time (= backend now); judges expiry + orders the event
	IN arg_lease_expires_at datetime(6),    -- reclaim: new ABSOLUTE lease deadline
	IN arg_lease_token varbinary(16)        -- reclaim: caller-minted NEW token (rotated in); NULL => read-only
)
proc:BEGIN
	DECLARE v_now datetime(6);              -- DB wall clock: updated_at AUDIT column only
	DECLARE v_current_event_ts datetime(6);
	DECLARE v_current_lease_token varbinary(16);
	DECLARE v_status tinyint unsigned;
	DECLARE v_lease_expires datetime(6);
	DECLARE v_payload mediumtext;
	DECLARE v_item_meta mediumtext;
	DECLARE v_checkpoint mediumtext;
	DECLARE v_head_missing tinyint(1) DEFAULT 0;
	DECLARE v_recovery_attempt int unsigned DEFAULT 0;

	DECLARE CONST_STATUS_WORKING tinyint unsigned DEFAULT 1;
	DECLARE CONST_STATUS_DONE tinyint unsigned DEFAULT 2;
	DECLARE CONST_STATUS_FAILED tinyint unsigned DEFAULT 3;
	DECLARE CONST_EVENT_CLAIMED tinyint unsigned DEFAULT 10;

	-- PR2 resume may MUTATE (reclaim grant), so it LOCKS the projection (PR1 was read-only).
	SET v_current_event_ts = NULL;
	SELECT `current_event_ts`, `current_lease_token`
		INTO v_current_event_ts, v_current_lease_token
		FROM `tb_singular_work_item`
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key
		FOR UPDATE;

	IF v_current_event_ts IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- Referenced head-history row: explicit presence check (dangling pointer = corruption).
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_head_missing = 1;
		SELECT `status`, `lease_expires_at`, `event_payload`, `item_meta`, `checkpoint_payload`
			INTO v_status, v_lease_expires, v_payload, v_item_meta, v_checkpoint
			FROM `tb_singular_work_item_history`
			WHERE `service_group` = arg_service_group
				AND `idempotency_key` = arg_idempotency_key
				AND `event_ts` = v_current_event_ts;
	END;
	IF v_head_missing = 1 THEN
		SIGNAL SQLSTATE '45001' SET MESSAGE_TEXT = 'SingularHeadHistoryMissing', MYSQL_ERRNO = 30001;
	END IF;

	-- TERMINAL replay FIRST: the recovery lease (incl. its token) is ignored here, so a malformed proposed
	-- token can never block a legitimate terminal replay.
	IF v_status = CONST_STATUS_DONE OR v_status = CONST_STATUS_FAILED THEN
		SELECT JSON_OBJECT(
			'outcome', 'terminal',
			'state', IF(v_status = CONST_STATUS_DONE, 'done', 'failed'),
			'payload', JSON_EXTRACT(v_payload, '$')   -- nested object document, not a JSON string
		) AS result;
		LEAVE proc;
	END IF;

	IF v_status <> CONST_STATUS_WORKING THEN
		-- DEFERRED/INDETERMINATE not reachable in PR2.
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidState';
	END IF;

	-- WORKING. A RECLAIM is requested iff a new token is supplied. Without one, this is a READ-ONLY resume:
	-- report ACTIVE (the caller reads lease_expires_at and decides whether to come back with a recovery lease).
	IF arg_lease_token IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'active', 'lease_expires_at', v_lease_expires) AS result;
		LEAVE proc;
	END IF;

	-- Reclaim requested -> the recovery inputs are REQUIRED + validated. Reached ONLY for a WORKING item, so
	-- a malformed proposed token can never reject a terminal replay (handled above).
	IF LENGTH(arg_lease_token) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseTokenInvalid';
	END IF;
	IF arg_lease_owner IS NULL OR LENGTH(arg_lease_owner) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseOwnerInvalid';
	END IF;
	IF arg_lease_meta IS NULL OR JSON_VALID(arg_lease_meta) = 0 OR JSON_TYPE(arg_lease_meta) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseMetaInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularEventTimeInvalid';
	END IF;
	IF arg_lease_expires_at IS NULL OR arg_lease_expires_at <= arg_event_ts THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidLeaseExpiry', MYSQL_ERRNO = 30003;
	END IF;

	-- LIVE lease (not yet expired at the caller's claim time) -> never steal; report ACTIVE (§6.6). Expiry is
	-- strict: at exactly lease_expires_at the lease is still live (mirrors start's `expires > event_ts`).
	IF NOT (arg_event_ts > v_lease_expires) THEN
		SELECT JSON_OBJECT('outcome', 'active', 'lease_expires_at', v_lease_expires) AS result;
		LEAVE proc;
	END IF;

	-- EXPIRED working -> GRANT a fresh attempt. Strict event-time monotonicity (the new claim MUST be after
	-- the item's last recorded event), then ROTATE the token + append a CLAIMED recovery event.
	IF arg_event_ts <= v_current_event_ts THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularEventTimeConflict', MYSQL_ERRNO = 30002;
	END IF;

	-- recovery_attempt = number of reclaims = count of CLAIMED events BEFORE this one (start is CLAIMED #1,
	-- so the first reclaim yields 1). Derived from the durable history; no separate counter column.
	SELECT COUNT(*) INTO v_recovery_attempt
		FROM `tb_singular_work_item_history`
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key
			AND `event_type` = CONST_EVENT_CLAIMED;

	SET v_now = UTC_TIMESTAMP(6);

	-- CLAIMED recovery event: new lease (owner/meta/expiry/token), carrying forward item_meta + checkpoint.
	-- event_payload stays the empty document `{}` (CLAIMED is non-terminal), consistent with start/extend.
	INSERT INTO `tb_singular_work_item_history` (
		`service_group`, `idempotency_key`, `event_ts`, `item_meta`,
		`lease_owner`, `lease_meta`, `lease_expires_at`,
		`event_type`, `status`, `event_payload`, `lease_token`, `checkpoint_payload`
	) VALUES (
		arg_service_group, arg_idempotency_key, arg_event_ts, v_item_meta,
		arg_lease_owner, arg_lease_meta, arg_lease_expires_at,
		CONST_EVENT_CLAIMED, CONST_STATUS_WORKING, '{}', arg_lease_token, v_checkpoint
	);

	UPDATE `tb_singular_work_item`
		SET `current_event_ts` = arg_event_ts,
			`current_lease_token` = arg_lease_token,   -- ROTATE: the prior holder's token is now stale (§4)
			`updated_at` = v_now
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key;

	SELECT JSON_OBJECT(
		'outcome', 'granted',
		'kind', 'reclaim',
		'lease_expires_at', arg_lease_expires_at,
		'recovery_attempt', CAST(v_recovery_attempt AS SIGNED),   -- JSON NUMBER (CAST avoids variable-string quoting)
		'checkpoint', JSON_EXTRACT(v_checkpoint, '$')   -- persisted context handed to the new attempt
	) AS result;
END $$
DELIMITER ;
