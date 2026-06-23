DELIMITER $$
-- Terminal SUCCESS transition, fenced by the capability TOKEN (not lease_owner).
-- Actionable-state contract: one SETTLED outcome (no Applied/Terminal split) — whether this
-- call performed the DONE write or the item was already settled under this token, the caller
-- delivers the authoritative result. A foreign/stale token is TOKEN_STALE; a missing row is
-- NOT_FOUND. Result: ONE discriminated JSON document (column `result`), an OBJECT keyed by `outcome`:
--   {"outcome":"settled","state":"done|failed","payload":{<authoritative payload document>}}
--   {"outcome":"token_stale"}  |  {"outcome":"not_found"}
-- Arm-inapplicable fields are OMITTED, never SQL/JSON null. The payload is embedded as a NESTED JSON
-- object (document semantics, not a JSON-in-a-string); the stored terminal record is immutable.
-- arg_response must be a non-NULL valid JSON OBJECT (the DB document contract); the gateway also
-- validates this object contract before SQL (InvalidJson) — this SIGNAL is the backend-side guarantee.
CREATE PROCEDURE `sp_singular_complete`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32),
	IN arg_lease_owner varbinary(16),       -- descriptive/audit only; varbinary so short values are NOT padded
	IN arg_lease_token varbinary(16),       -- the authority
	IN arg_event_ts datetime(6),            -- 0.5: caller-supplied settle time (strictly monotonic)
	IN arg_response mediumtext
)
proc:BEGIN
	DECLARE v_now datetime(6);              -- DB wall clock: updated_at AUDIT column only
	DECLARE v_current_event_ts datetime(6);
	DECLARE v_current_lease_token varbinary(16);
	DECLARE v_terminal_lease_token varbinary(16);
	DECLARE v_existing_status tinyint unsigned;
	DECLARE v_existing_item_meta mediumtext;
	DECLARE v_existing_lease_meta mediumtext;
	DECLARE v_existing_checkpoint mediumtext;
	DECLARE v_existing_payload mediumtext;
	DECLARE v_head_missing tinyint(1) DEFAULT 0;
	DECLARE v_new_event_ts datetime(6);
	DECLARE v_result_code tinyint unsigned DEFAULT 0;
	DECLARE v_result_status tinyint unsigned DEFAULT NULL;
	DECLARE v_result_payload mediumtext DEFAULT NULL;

	DECLARE CONST_STATUS_WORKING tinyint unsigned DEFAULT 1;
	DECLARE CONST_STATUS_DONE tinyint unsigned DEFAULT 2;
	DECLARE CONST_STATUS_FAILED tinyint unsigned DEFAULT 3;

	DECLARE CONST_RESULT_SETTLED tinyint unsigned DEFAULT 1;
	DECLARE CONST_RESULT_TOKEN_STALE tinyint unsigned DEFAULT 2;
	DECLARE CONST_RESULT_NOT_FOUND tinyint unsigned DEFAULT 3;

	DECLARE CONST_EVENT_COMPLETED tinyint unsigned DEFAULT 20;

	-- Token validation: required, exactly 16 bytes. Invalid input -> SIGNAL.
	IF arg_lease_token IS NULL OR LENGTH(arg_lease_token) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseTokenInvalid';
	END IF;

	-- Owner is descriptive but PERSISTED non-null: require exactly 16 bytes (varbinary, so a short
	-- value is rejected here rather than silently zero-padded by a binary(16) parameter).
	IF arg_lease_owner IS NULL OR LENGTH(arg_lease_owner) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseOwnerInvalid';
	END IF;

	-- Terminal payload is REQUIRED and must be a JSON OBJECT (the DB document contract): no
	-- NULL/empty, no JSON null/array/scalar. The empty result document is `{}`.
	IF arg_response IS NULL OR JSON_VALID(arg_response) = 0 OR JSON_TYPE(arg_response) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularResponseInvalid';
	END IF;

	-- Lock the head row and read the tokens. A missing row is a returned result code (NOT_FOUND).
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
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- Referenced head-history row: explicit presence check. The projection exists (above), so an
	-- absent head event is a dangling pointer = corruption, surfaced as SIGNAL '45001' rather than
	-- inferred from NULL vars.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_head_missing = 1;
		SELECT
			`status`,
			`item_meta`,
			`lease_meta`,
			`checkpoint_payload`,
			`event_payload`
		INTO
			v_existing_status,
			v_existing_item_meta,
			v_existing_lease_meta,
			v_existing_checkpoint,
			v_existing_payload
		FROM `tb_singular_work_item_history`
		WHERE `service_group` = arg_service_group
			AND `idempotency_key` = arg_idempotency_key
			AND `event_ts` = v_current_event_ts;
	END;
	IF v_head_missing = 1 THEN
		SIGNAL SQLSTATE '45001' SET MESSAGE_TEXT = 'SingularHeadHistoryMissing', MYSQL_ERRNO = 30001;
	END IF;

	SET v_now = UTC_TIMESTAMP(6);

	IF v_existing_status = CONST_STATUS_WORKING THEN
		IF NOT (arg_lease_token <=> v_current_lease_token) THEN
			SET v_result_code = CONST_RESULT_TOKEN_STALE;
		ELSE
			-- 0.5 strict monotonicity: the caller's settle time MUST be strictly after the item's last
			-- recorded event (equal disallowed). Violation -> EventTimeConflict (errno 30002). The
			-- caller's time is used verbatim — never silently replaced or bumped to the DB clock.
			IF arg_event_ts IS NULL OR arg_event_ts <= v_current_event_ts THEN
				SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularEventTimeConflict', MYSQL_ERRNO = 30002;
			END IF;
			SET v_new_event_ts = arg_event_ts;

			INSERT INTO `tb_singular_work_item_history` (
				`service_group`, `idempotency_key`, `event_ts`, `item_meta`,
				`lease_owner`, `lease_meta`, `lease_expires_at`,
				`event_type`, `status`, `event_payload`, `lease_token`, `checkpoint_payload`
			) VALUES (
				arg_service_group, arg_idempotency_key, v_new_event_ts, v_existing_item_meta,
				arg_lease_owner, v_existing_lease_meta, NULL,
				CONST_EVENT_COMPLETED, CONST_STATUS_DONE, arg_response, arg_lease_token, v_existing_checkpoint
			);

			UPDATE `tb_singular_work_item`
			SET
				`current_event_ts` = v_new_event_ts,
				`updated_at` = v_now,
				`terminal_lease_token` = arg_lease_token,   -- pin the writer
				`current_lease_token` = NULL,               -- lease consumed
				`checkpoint_payload` = v_existing_checkpoint
			WHERE `service_group` = arg_service_group
				AND `idempotency_key` = arg_idempotency_key;

			SET v_result_code = CONST_RESULT_SETTLED;
			SET v_result_status = CONST_STATUS_DONE;
			SET v_result_payload = arg_response;            -- authoritative payload just written
		END IF;
	ELSEIF v_existing_status = CONST_STATUS_DONE OR v_existing_status = CONST_STATUS_FAILED THEN
		-- Already settled: the writer's own token learns the authoritative terminal state+payload
		-- (covers same-token replay AND a cross-terminal complete() on a FAILED item). Any other
		-- token is stale.
		IF arg_lease_token <=> v_terminal_lease_token THEN
			SET v_result_code = CONST_RESULT_SETTLED;
			SET v_result_status = v_existing_status;
			SET v_result_payload = v_existing_payload;
		ELSE
			SET v_result_code = CONST_RESULT_TOKEN_STALE;
		END IF;
	ELSE
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularInvalidState';
	END IF;

	IF v_result_code = CONST_RESULT_SETTLED THEN
		SELECT JSON_OBJECT(
			'outcome', 'settled',
			'state', IF(v_result_status = CONST_STATUS_DONE, 'done', 'failed'),
			'payload', JSON_EXTRACT(v_result_payload, '$')   -- nested object document, not a JSON string
		) AS result;
	ELSE
		-- Only TOKEN_STALE reaches here (NOT_FOUND already returned above).
		SELECT JSON_OBJECT('outcome', 'token_stale') AS result;
	END IF;
END $$
DELIMITER ;
