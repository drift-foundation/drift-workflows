DELIMITER $$
-- Settle a remote operation on durable SUCCESS, atomically (microflows_design.md
-- §2.5): one transaction records the result, creates the Checkpoint, and
-- advances the continuation. The Checkpoint exists ONLY after a success is
-- durably recorded (§3).
--
-- Fenced by the executor lease (§24.3). Idempotent: a replay after a
-- crash-before-settle (or a lost-ack reconcile) finds the operation already
-- status=succeeded and returns 'already_settled' with the recorded result —
-- the operation is settled exactly once. Time discipline (§24.4): caller
-- event_ts strictly greater than current_event_ts; event_ts is the ordering key.
--
-- arg_is_final distinguishes the LAST operation of a (manual-IR) plan from an
-- intermediate one:
--   final (1)        -> drive the workflow to completed (forward -> completed),
--                       clear the lease, emit 'workflow_completed'.
--   intermediate (0) -> stay forward(1) RETAINING the lease (the same drive
--                       proceeds to the next operation; on crash the lease
--                       expires and another worker resumes from the durable
--                       operation/checkpoint state), advance the continuation,
--                       emit 'operation_settled'.
-- A single-operation plan settles with is_final=1 (the original behavior).
--
-- 1b.0a step 3: on a final settle, arg_workflow_return_json is the workflow's
-- AUTHORITATIVE typed return (durable, separate from arg_result_json — a later
-- operation's raw result). Written in the SAME UPDATE that flips state->completed,
-- so op result + workflow return + completed state land in one commit. NULL is
-- required on a non-final settle (arg_is_final=0).
CREATE PROCEDURE `sp_mf_operation_settle`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
	IN arg_checkpoint_seq int,
	IN arg_result_json mediumtext,
	IN arg_checkpoint_payload mediumtext,
	-- The workflow's TERMINAL RETURN (1b.0a step 3, the authoritative typed workflow
	-- return — separate from arg_result_json, the per-operation result). Required
	-- (a JSON object) iff arg_is_final=1; MUST be SQL NULL when arg_is_final=0 (a
	-- non-final settle must never smuggle a return value). Written atomically with
	-- completion in the SAME final-settle UPDATE below — never a second write.
	IN arg_workflow_return_json mediumtext,
	IN arg_new_continuation mediumtext,
	IN arg_event_ts datetime(6),
	IN arg_event_payload mediumtext,
	IN arg_is_final tinyint(1)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_op_status tinyint;
	DECLARE v_op_id varbinary(16);
	DECLARE v_op_name varchar(128);
	DECLARE v_existing_result mediumtext;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_op_missing tinyint(1) DEFAULT 0;
	DECLARE v_plan_length int DEFAULT NULL;

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
	IF arg_is_final IS NULL OR arg_is_final NOT IN (0, 1) THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfIsFinalInvalid';
	END IF;
	-- arg_workflow_return_json validity depends on arg_is_final (checked here, not
	-- in a structured outcome below, matching every other entry check above): a
	-- final settle requires a JSON-object return; a non-final settle requires NULL.
	IF arg_is_final = 1 AND (arg_workflow_return_json IS NULL
			OR JSON_VALID(arg_workflow_return_json) = 0 OR JSON_TYPE(arg_workflow_return_json) <> 'OBJECT') THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowReturnJsonInvalid';
	END IF;
	IF arg_is_final = 0 AND arg_workflow_return_json IS NOT NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowReturnJsonUnexpected';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_ts`
		INTO v_owner, v_token, v_state, v_event_ts
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- DURABLE plan conformance (for a pinned plan): the operation must lie WITHIN the
	-- committed plan and map to its own checkpoint, and finality is DERIVED from
	-- plan_length — none trusted from the caller, so a runner defect cannot settle an
	-- out-of-plan step or complete the workflow early. These depend only on the plan +
	-- the supplied args, so they run BEFORE the operation load (a seq outside the plan
	-- has no operation row, and must read as a plan_violation, not operation_not_found).
	-- Legacy (unpinned) workflows have no plan row and keep caller-supplied finality.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_plan_length = NULL;
		SELECT `plan_length` INTO v_plan_length FROM `tb_mf_workflow_plan`
		WHERE `workflow_id` = arg_workflow_id;
	END;
	IF v_plan_length IS NOT NULL THEN
		IF arg_operation_seq < 1 OR arg_operation_seq > v_plan_length THEN
			SELECT JSON_OBJECT('outcome', 'plan_violation', 'reason', 'seq_out_of_range',
				'plan_length', CAST(v_plan_length AS SIGNED)) AS result;
			LEAVE proc;
		END IF;
		IF arg_checkpoint_seq <> arg_operation_seq THEN
			SELECT JSON_OBJECT('outcome', 'plan_violation', 'reason', 'checkpoint_mismatch',
				'plan_length', CAST(v_plan_length AS SIGNED)) AS result;
			LEAVE proc;
		END IF;
		-- Finality: the runner may DOWNGRADE a plan-length op to non-final (a settled result that
		-- branches to an authored `fail` must be CHECKPOINTED so compensation can include it, not
		-- completed), but may NEVER mark final before plan end. So reject only an EARLY final claim
		-- (is_final at seq < plan_length); is_final=0 at plan_length is the legitimate fail-path case.
		IF arg_is_final = 1 AND arg_operation_seq <> v_plan_length THEN
			SELECT JSON_OBJECT('outcome', 'plan_violation', 'reason', 'finality_early',
				'plan_length', CAST(v_plan_length AS SIGNED)) AS result;
			LEAVE proc;
		END IF;
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

	-- Clock skew: defer until current_event_ts + margin (see request SP).
	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;
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

	IF arg_is_final = 1 THEN
		-- Last operation: complete the workflow (forward -> completed), clear lease.
		-- workflow_return_json is written in this SAME statement (never a second
		-- write) — the final op result, the workflow return, and state=4 land
		-- together in the one open transaction this proc call commits (host-owned;
		-- see _finish_stmt_and_commit/rpc.commit).
		UPDATE `tb_mf_workflow`
		SET `continuation` = arg_new_continuation,
		    `state` = 4,
		    `current_disposition` = 1,
		    `current_event_ts` = arg_event_ts,
		    `lease_owner` = NULL,
		    `lease_expires_at` = NULL,
		    `workflow_return_json` = arg_workflow_return_json,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, arg_event_ts, 'workflow_completed', arg_executor, NULL, arg_event_payload
		);
	ELSE
		-- Intermediate operation: stay forward(1), advance the continuation, RETAIN
		-- the lease (the same drive proceeds to the next operation). Disposition
		-- stays unchanged (the workflow is not yet completed).
		UPDATE `tb_mf_workflow`
		SET `continuation` = arg_new_continuation,
		    `current_event_ts` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, arg_event_ts, 'operation_settled', arg_executor, NULL, arg_event_payload
		);
	END IF;

	SELECT JSON_OBJECT('outcome', 'settled', 'result', JSON_EXTRACT(arg_result_json, '$')) AS result;
END $$
DELIMITER ;
