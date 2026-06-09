DELIMITER $$
-- Settle a remote operation on durable SUCCESS, atomically (microflows_design.md
-- §2.5): one transaction records the result, creates the Checkpoint, advances
-- the continuation, and completes the workflow. The Checkpoint exists ONLY
-- after a success is durably recorded (§3).
--
-- Fenced by the executor lease (§24.3). Idempotent: a replay after a
-- crash-before-settle (or a lost-ack reconcile) finds the operation already
-- status=succeeded and returns 'already_settled' with the recorded result —
-- the operation is settled exactly once. Time discipline (§24.4): caller
-- event_ts strictly greater than current_event_ts; ordering is event_seq.
--
-- First-slice scope: the workflow has one operation, so settle also drives the
-- workflow to completed (forward -> completed) and clears the lease.
CREATE PROCEDURE `sp_mf_operation_settle`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
	IN arg_checkpoint_seq int,
	IN arg_result_json mediumtext,
	IN arg_checkpoint_payload mediumtext,
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
	DECLARE v_op_status tinyint;
	DECLARE v_op_id varbinary(16);
	DECLARE v_op_name varchar(128);
	DECLARE v_existing_result mediumtext;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_op_missing tinyint(1) DEFAULT 0;

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
	IF arg_checkpoint_seq IS NULL OR arg_checkpoint_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfCheckpointSeqInvalid';
	END IF;
	IF arg_result_json IS NULL OR JSON_VALID(arg_result_json) = 0 OR JSON_TYPE(arg_result_json) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfResultJsonInvalid';
	END IF;
	IF arg_checkpoint_payload IS NULL OR JSON_VALID(arg_checkpoint_payload) = 0 OR JSON_TYPE(arg_checkpoint_payload) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfCheckpointPayloadInvalid';
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

	-- Load the operation row.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `status`, `operation_id`, `operation_name`, `result_json`
		INTO v_op_status, v_op_id, v_op_name, v_existing_result
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END;

	IF v_op_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'operation_not_found') AS result;
		LEAVE proc;
	END IF;

	-- Identify the remote operation: the supplied operation_id MUST match the
	-- stored one, so a runner bug cannot settle this operation with a response
	-- belonging to a different operation_id. Checked before everything else
	-- (incl. the idempotent already-settled read).
	IF NOT (v_op_id <=> arg_operation_id) THEN
		SELECT JSON_OBJECT('outcome', 'operation_conflict') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent replay: already settled -> return the recorded result. (Before
	-- the fence check, because settle clears the lease — a lost-ack settle
	-- retry has no live token and must still resolve to already_settled.)
	IF v_op_status = 2 THEN
		SELECT JSON_OBJECT('outcome', 'already_settled', 'result', JSON_EXTRACT(v_existing_result, '$')) AS result;
		LEAVE proc;
	END IF;

	-- Fence: must hold the lease (owner + token) on a forward workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew') AS result;
		LEAVE proc;
	END IF;

	SET v_event_seq = v_event_seq + 1;

	UPDATE `tb_mf_operation`
	SET `status` = 2,
	    `result_json` = arg_result_json,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;

	INSERT INTO `tb_mf_workflow_checkpoint` (
		`workflow_id`, `seq`, `operation_name`, `operation_id`, `payload`,
		`reversal_state`, `created_at`, `updated_at`
	) VALUES (
		arg_workflow_id, arg_checkpoint_seq, v_op_name, v_op_id, arg_checkpoint_payload,
		1, arg_event_ts, arg_event_ts
	);

	-- Complete the workflow (forward -> completed) and clear the lease.
	UPDATE `tb_mf_workflow`
	SET `continuation` = arg_new_continuation,
	    `state` = 4,
	    `current_disposition` = 1,
	    `current_event_seq` = v_event_seq,
	    `current_event_ts` = arg_event_ts,
	    `lease_owner` = NULL,
	    `lease_expires_at` = NULL,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, v_event_seq, arg_event_ts, 'workflow_completed', arg_executor, NULL, arg_event_payload
	);

	SELECT JSON_OBJECT('outcome', 'settled', 'result', JSON_EXTRACT(arg_result_json, '$')) AS result;
END $$
DELIMITER ;
