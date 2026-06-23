DELIMITER $$
-- Extend the live lease's expiry, fenced by the capability TOKEN. Extend-only: no
-- token rotation, no lease_meta/checkpoint mutation (hence the precise name -- it is
-- not a "renew"). Only the token holding the live lease may extend; a stale/foreign
-- token -> TOKEN_STALE. A terminal item -> TERMINAL *only for the token that wrote it*
-- (the writer learns the authoritative state+payload); any other token on a terminal
-- item is TOKEN_STALE -- a stale worker must be suppressed, never handed the result.
-- A missing row -> NOT_FOUND. lease_owner is audit only. Result: ONE discriminated JSON document
-- (column `result`), an OBJECT keyed by `outcome` — {"outcome":"extended"} | {"outcome":"terminal",
-- "state":"done|failed","payload":{...}} | {"outcome":"token_stale"} | {"outcome":"not_found"};
-- arm-inapplicable fields omitted, never SQL/JSON null; payload is a NESTED object.
CREATE PROCEDURE `sp_singular_extend_lease`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32),
	IN arg_lease_owner varbinary(16),       -- descriptive/audit only; varbinary so short values are NOT padded
	IN arg_lease_token varbinary(16),       -- the authority
	IN arg_event_ts datetime(6),            -- 0.5: caller-supplied event time (strictly monotonic)
	IN arg_lease_expires_at datetime(6)     -- 0.5: caller-supplied ABSOLUTE new lease deadline
)
proc:BEGIN
	DECLARE v_now datetime(6);              -- DB wall clock: updated_at AUDIT column only
	DECLARE v_current_event_ts datetime(6);
	DECLARE v_current_lease_token varbinary(16);
	DECLARE v_terminal_lease_token varbinary(16);
	DECLARE v_new_event_ts datetime(6);
	DECLARE v_existing_status tinyint unsigned;
	DECLARE v_existing_lease_expires datetime(6);
	DECLARE v_existing_item_meta mediumtext;
	DECLARE v_existing_lease_meta mediumtext;
	DECLARE v_existing_payload mediumtext;
	DECLARE v_existing_checkpoint mediumtext;
	DECLARE v_head_missing tinyint(1) DEFAULT 0;
	DECLARE v_desired_lease_expires datetime(6);
	DECLARE v_result_code tinyint unsigned DEFAULT 0;
	DECLARE v_result_status tinyint unsigned DEFAULT NULL;
	DECLARE v_result_payload mediumtext;

	DECLARE CONST_STATUS_WORKING tinyint unsigned DEFAULT 1;
	DECLARE CONST_STATUS_DONE tinyint unsigned DEFAULT 2;
	DECLARE CONST_STATUS_FAILED tinyint unsigned DEFAULT 3;

	DECLARE CONST_RESULT_EXTENDED tinyint unsigned DEFAULT 1;
	DECLARE CONST_RESULT_TOKEN_STALE tinyint unsigned DEFAULT 2;
	DECLARE CONST_RESULT_NOT_FOUND tinyint unsigned DEFAULT 3;
	DECLARE CONST_RESULT_TERMINAL tinyint unsigned DEFAULT 4;

	DECLARE CONST_EVENT_EXTENDED tinyint unsigned DEFAULT 11;

	IF arg_lease_token IS NULL OR LENGTH(arg_lease_token) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseTokenInvalid';
	END IF;

	-- Owner is descriptive but PERSISTED non-null: require exactly 16 bytes (varbinary, not padded).
	IF arg_lease_owner IS NULL OR LENGTH(arg_lease_owner) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseOwnerInvalid';
	END IF;

	-- 0.5 absolute-time contract: caller supplies the event time + the new ABSOLUTE deadline. The
	-- monotonicity + deadline checks are deferred to the WORKING write path below (after the
	-- token/terminal fencing) so a stale/terminal token still gets its own outcome, not a time error.
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularEventTimeInvalid';
	END IF;

	SET v_current_event_ts = NULL;
	SELECT
		`current_event_ts`,
		`current_lease_token`,
		`terminal_lease_token`
	INTO
		v_current_event_ts,
		v_current_lease_token,
		v_terminal_lease_token
	FROM `tb_singular_work_item`
	WHERE `service_group` = arg_service_group
		AND `idempotency_key` = arg_idempotency_key
	FOR UPDATE;

	IF v_current_event_ts IS NULL THEN
		SET v_result_code = CONST_RESULT_NOT_FOUND;
	END IF;

	IF v_result_code = 0 THEN
		-- Referenced head-history row: explicit presence check (dangling pointer = corruption).
		BEGIN
			DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_head_missing = 1;
			SELECT
				`status`,
				`lease_expires_at`,
				`item_meta`,
				`lease_meta`,
				`event_payload`,
				`checkpoint_payload`
			INTO
				v_existing_status,
				v_existing_lease_expires,
				v_existing_item_meta,
				v_existing_lease_meta,
				v_existing_payload,
				v_existing_checkpoint
			FROM `tb_singular_work_item_history`
			WHERE `service_group` = arg_service_group
				AND `idempotency_key` = arg_idempotency_key
				AND `event_ts` = v_current_event_ts;
		END;
		IF v_head_missing = 1 THEN
			SIGNAL SQLSTATE '45001' SET MESSAGE_TEXT = 'SingularHeadHistoryMissing', MYSQL_ERRNO = 30001;
		END IF;

		SET v_now = UTC_TIMESTAMP(6);

		IF v_existing_status = CONST_STATUS_DONE OR v_existing_status = CONST_STATUS_FAILED THEN
			-- Settled: only the writer's own token gets the authoritative TERMINAL;
			-- any other token is fenced out as TOKEN_STALE (suppress the stale worker).
			IF arg_lease_token <=> v_terminal_lease_token THEN
				SET v_result_code = CONST_RESULT_TERMINAL;
				SET v_result_status = v_existing_status;
				SET v_result_payload = v_existing_payload;
			ELSE
				SET v_result_code = CONST_RESULT_TOKEN_STALE;
			END IF;
		ELSEIF v_existing_status <> CONST_STATUS_WORKING THEN
			SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidState';
		ELSEIF NOT (arg_lease_token <=> v_current_lease_token) THEN
			SET v_result_code = CONST_RESULT_TOKEN_STALE;
		END IF;
	END IF;

	IF v_result_code = 0 THEN
		-- 0.5 strict monotonicity + absolute deadline, enforced on the WORKING extend write:
		--   event_ts <= last recorded event   -> EventTimeConflict   (errno 30002)
		--   lease_expires_at <= event_ts       -> InvalidLeaseExpiry  (errno 30003)
		-- The caller's times are authoritative and used verbatim — the deadline is NOT floored to the
		-- old one (the caller owns absolute time in 0.5; "never shorten" is a caller-side policy now).
		IF arg_event_ts <= v_current_event_ts THEN
			SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularEventTimeConflict', MYSQL_ERRNO = 30002;
		END IF;
		IF arg_lease_expires_at IS NULL OR arg_lease_expires_at <= arg_event_ts THEN
			SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidLeaseExpiry', MYSQL_ERRNO = 30003;
		END IF;
		SET v_new_event_ts = arg_event_ts;
		SET v_desired_lease_expires = arg_lease_expires_at;

		INSERT INTO `tb_singular_work_item_history` (
			`service_group`,
			`idempotency_key`,
			`event_ts`,
			`item_meta`,
			`lease_owner`,
			`lease_meta`,
			`lease_expires_at`,
			`event_type`,
			`status`,
			`event_payload`,
			`lease_token`,
			`checkpoint_payload`
		) VALUES (
			arg_service_group,
			arg_idempotency_key,
			v_new_event_ts,
			v_existing_item_meta,
			arg_lease_owner,
			v_existing_lease_meta,
			v_desired_lease_expires,
			CONST_EVENT_EXTENDED,
			CONST_STATUS_WORKING,
			'{}',                    -- event_payload: EXTENDED is a non-terminal event -> empty document
			arg_lease_token,
			v_existing_checkpoint
		);

		UPDATE `tb_singular_work_item`
		SET
			`current_event_ts` = v_new_event_ts,
			`updated_at` = v_now
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key;

		SET v_result_code = CONST_RESULT_EXTENDED;
	END IF;

	IF v_result_code = CONST_RESULT_EXTENDED THEN
		-- 0.5: return the new ABSOLUTE deadline so the gateway threads an updated WorkLease.
		SELECT JSON_OBJECT('outcome', 'extended', 'lease_expires_at', v_desired_lease_expires) AS result;
	ELSEIF v_result_code = CONST_RESULT_TOKEN_STALE THEN
		SELECT JSON_OBJECT('outcome', 'token_stale') AS result;
	ELSEIF v_result_code = CONST_RESULT_NOT_FOUND THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
	ELSE
		SELECT JSON_OBJECT(
			'outcome', 'terminal',
			'state', IF(v_result_status = CONST_STATUS_DONE, 'done', 'failed'),
			'payload', JSON_EXTRACT(v_result_payload, '$')   -- nested object document, not a JSON string
		) AS result;
	END IF;
END $$
DELIMITER ;
