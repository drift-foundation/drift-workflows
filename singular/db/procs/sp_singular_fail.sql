DELIMITER $$
-- Terminal FAILURE transition (Rev #4: terminal-only). Mirror of sp_singular_complete under the
-- same actionable-state contract: one SETTLED outcome (no Applied/Terminal split), TOKEN_STALE for
-- a foreign/stale token, NOT_FOUND for a missing row. Result: ONE discriminated JSON document
-- (column `result`), an OBJECT keyed by `outcome` — {"outcome":"settled","state":"done|failed",
-- "payload":{...}} | {"outcome":"token_stale"} | {"outcome":"not_found"}; arm-inapplicable fields
-- omitted; payload is a NESTED object. arg_error must be a non-NULL valid JSON OBJECT (the document contract).
CREATE PROCEDURE `sp_singular_fail`(
	IN arg_service_group varchar(64),
	IN arg_idempotency_key varbinary(32),
	IN arg_lease_owner varbinary(16),       -- descriptive/audit only; varbinary so short values are NOT padded
	IN arg_lease_token varbinary(16),       -- the authority
	IN arg_error mediumtext
)
proc:BEGIN
	DECLARE v_now datetime(6);
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

	DECLARE CONST_EVENT_FAILED tinyint unsigned DEFAULT 40;

	IF arg_lease_token IS NULL OR LENGTH(arg_lease_token) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseTokenInvalid';
	END IF;

	-- Owner is descriptive but PERSISTED non-null: require exactly 16 bytes (varbinary, not padded).
	IF arg_lease_owner IS NULL OR LENGTH(arg_lease_owner) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularLeaseOwnerInvalid';
	END IF;

	-- Terminal payload is REQUIRED and must be a JSON OBJECT (the DB document contract): no
	-- NULL/empty, no JSON null/array/scalar.
	IF arg_error IS NULL OR JSON_VALID(arg_error) = 0 OR JSON_TYPE(arg_error) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'SingularErrorInvalid';
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
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- Referenced head-history row: explicit presence check (dangling pointer = corruption).
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
			SET v_new_event_ts = v_now;
			IF v_new_event_ts <= v_current_event_ts THEN
				SET v_new_event_ts = DATE_ADD(v_current_event_ts, INTERVAL 1 MICROSECOND);
			END IF;

			INSERT INTO `tb_singular_work_item_history` (
				`service_group`, `idempotency_key`, `event_ts`, `item_meta`,
				`lease_owner`, `lease_meta`, `lease_expires_at`,
				`event_type`, `status`, `event_payload`, `lease_token`, `checkpoint_payload`
			) VALUES (
				arg_service_group, arg_idempotency_key, v_new_event_ts, v_existing_item_meta,
				arg_lease_owner, v_existing_lease_meta, NULL,
				CONST_EVENT_FAILED, CONST_STATUS_FAILED, arg_error, arg_lease_token, v_existing_checkpoint
			);

			UPDATE `tb_singular_work_item`
			SET
				`current_event_ts` = v_new_event_ts,
				`updated_at` = v_now,
				`terminal_lease_token` = arg_lease_token,
				`current_lease_token` = NULL,
				`checkpoint_payload` = v_existing_checkpoint
			WHERE `service_group` = arg_service_group
				AND `idempotency_key` = arg_idempotency_key;

			SET v_result_code = CONST_RESULT_SETTLED;
			SET v_result_status = CONST_STATUS_FAILED;
			SET v_result_payload = arg_error;
		END IF;
	ELSEIF v_existing_status = CONST_STATUS_DONE OR v_existing_status = CONST_STATUS_FAILED THEN
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
