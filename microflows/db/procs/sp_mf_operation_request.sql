DELIMITER $$
-- Persist a remote operation REQUEST together with the suspended continuation,
-- atomically, BEFORE dispatch (microflows_design.md §2.5). One transaction:
-- insert the operation row (status=requested) + advance the workflow
-- continuation + append the audit event. After a crash, recovery finds the
-- request and reconciles by operation_id.
--
-- Fenced by the executor lease (owner + fencing token + forward state), like
-- every publication (§24.3). Idempotent on the STABLE command identity
-- (workflow_id, operation_seq): a replay loads the stored row and returns its
-- AUTHORITATIVE operation_id only when the supplied immutable fields
-- (operation_id, operation_name, input_hash) match; otherwise operation_conflict.
-- The persisted workflow continuation is the authority for resume position, so
-- a replay does not re-advance it. Time discipline (§24.4): caller event_ts
-- strictly greater than current_event_ts (else event_time_skew); event_seq
-- orders. input_hash is the CANONICAL input identity (the dispatcher hashes
-- the canonical input), so it is the comparison of record for the input.
CREATE PROCEDURE `sp_mf_operation_request`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
	IN arg_operation_name varchar(128),
	IN arg_schema_version int,
	IN arg_input_json mediumtext,
	IN arg_input_hash varchar(64),
	IN arg_new_continuation mediumtext,
	IN arg_event_ts datetime(6),
	IN arg_event_payload mediumtext
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_op_missing tinyint(1) DEFAULT 0;
	DECLARE v_ex_op_id varbinary(16);
	DECLARE v_ex_op_name varchar(128);
	DECLARE v_ex_schema_version int;
	DECLARE v_ex_input_hash varchar(64);

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_executor IS NULL OR LENGTH(arg_executor) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfExecutorInvalid';
	END IF;
	IF arg_fencing_token IS NULL OR arg_fencing_token < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfFencingTokenInvalid';
	END IF;
	IF arg_operation_seq IS NULL OR arg_operation_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationSeqInvalid';
	END IF;
	IF arg_operation_id IS NULL OR LENGTH(arg_operation_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationIdInvalid';
	END IF;
	IF arg_operation_name IS NULL OR arg_operation_name = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationNameInvalid';
	END IF;
	IF arg_schema_version IS NULL OR arg_schema_version < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfSchemaVersionInvalid';
	END IF;
	IF arg_input_hash IS NULL OR arg_input_hash = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfInputHashInvalid';
	END IF;
	IF arg_input_json IS NULL OR JSON_VALID(arg_input_json) = 0 OR JSON_TYPE(arg_input_json) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfInputJsonInvalid';
	END IF;
	IF arg_new_continuation IS NULL OR JSON_VALID(arg_new_continuation) = 0 OR JSON_TYPE(arg_new_continuation) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfContinuationInvalid';
	END IF;
	IF arg_event_payload IS NULL OR JSON_VALID(arg_event_payload) = 0 OR JSON_TYPE(arg_event_payload) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventPayloadInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_seq`, `current_event_ts`
		INTO v_owner, v_token, v_state, v_event_seq, v_event_ts
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- Fence FIRST — before BOTH new-request and replay handling. A stale
	-- executor that lost the lease must NOT receive 'exists' and proceed to
	-- dispatch. Recovery re-claims a fresh valid token, so legitimate replay
	-- is unaffected. (Request does not clear the lease, so the holder retains
	-- its token across a lost-ack request retry.)
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent replay on the stable command identity (workflow_id,
	-- operation_seq). The workflow row is already locked FOR UPDATE, so
	-- concurrent requests for this workflow are serialized — no second lock
	-- needed. Verify the supplied immutable fields match the stored request.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `operation_id`, `operation_name`, `schema_version`, `input_hash`
		INTO v_ex_op_id, v_ex_op_name, v_ex_schema_version, v_ex_input_hash
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq
		LIMIT 1;
	END;

	IF v_op_missing = 0 THEN
		IF NOT (v_ex_op_id <=> arg_operation_id
		        AND v_ex_op_name <=> arg_operation_name
		        AND v_ex_schema_version <=> arg_schema_version
		        AND v_ex_input_hash <=> arg_input_hash) THEN
			SELECT JSON_OBJECT('outcome', 'operation_conflict') AS result;
			LEAVE proc;
		END IF;
		SELECT JSON_OBJECT('outcome', 'exists', 'operation_id', LOWER(HEX(v_ex_op_id))) AS result;
		LEAVE proc;
	END IF;

	-- Strictly increasing event time; a non-increasing value is clock skew.
	-- Expose a deferral deadline based on the LAST ACCEPTED event time (not
	-- db_now): defer until current_event_ts + margin, so retrying before the
	-- clock catches up cannot repeat the same skew.
	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	SET v_event_seq = v_event_seq + 1;

	INSERT INTO `tb_mf_operation` (
		`workflow_id`, `operation_seq`, `operation_id`, `operation_name`, `schema_version`,
		`input_json`, `input_hash`, `status`, `result_json`, `created_at`, `updated_at`
	) VALUES (
		arg_workflow_id, arg_operation_seq, arg_operation_id, arg_operation_name, arg_schema_version,
		arg_input_json, arg_input_hash, 1, NULL, arg_event_ts, arg_event_ts
	);

	UPDATE `tb_mf_workflow`
	SET `continuation` = arg_new_continuation,
	    `current_event_seq` = v_event_seq,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, v_event_seq, arg_event_ts, 'operation_requested', arg_executor, NULL, arg_event_payload
	);

	SELECT JSON_OBJECT('outcome', 'requested', 'operation_id', LOWER(HEX(arg_operation_id))) AS result;
END $$
DELIMITER ;
