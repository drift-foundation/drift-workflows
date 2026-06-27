DELIMITER $$
-- Begin durable REVERSAL on a definite forward failure (microflows_design.md
-- §3.1, §6): forward(1) -> reversing(2), direction reverse(2), disposition
-- failed(2), so the committed checkpoint stack is compensated in reverse order.
--
-- The triggering command is identified DURABLY: the caller passes the forward
-- operation (seq + stable id) whose definite rejection caused this, and the proc
-- verifies inside the lock that the operation exists, matches the supplied id,
-- and is still 'requested' (a settled op SUCCEEDED and must never drive
-- compensation; reversal is begun only from a durable, un-settled request — never
-- from an uncertain forward outcome, §3.1).
--
-- If there are NO active checkpoints there is nothing to compensate -> straight
-- to reversed(5) (trivial unwind), lease cleared. Otherwise -> reversing(2), the
-- lease is RETAINED (the holder drives the reverse loop) and the continuation is
-- set to the reverse cursor at the TOP (highest-seq) active checkpoint.
--
-- Fenced + time-disciplined. Idempotent: already reversing/reversed returns that
-- outcome lease-independently (a retry after the lease changed still resolves).
CREATE PROCEDURE `sp_mf_workflow_begin_reversal`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
	IN arg_event_ts datetime(6),
	IN arg_reason varchar(190)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_trigger varbinary(16);
	DECLARE v_op_id varbinary(16);
	DECLARE v_op_status tinyint;
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_op_missing tinyint(1) DEFAULT 0;
	DECLARE v_term_reason varchar(190) DEFAULT NULL;

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
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;
	IF arg_reason IS NULL OR arg_reason = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReasonInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_seq`, `current_event_ts`, `reversal_trigger_operation_id`, `terminal_reason`
		INTO v_owner, v_token, v_state, v_event_seq, v_event_ts, v_trigger, v_term_reason
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent replay BOUND to the trigger, recognized across ALL reverse states
	-- (lease-independent). Once the begin command committed, the trigger op id is
	-- persisted; a retry then returns already_begun with the CURRENT state — whether
	-- reversing(2), blocked_resolution(3), reversed(5), resolved_exception(6), or failed(7) —
	-- never fence_lost. A different operation is trigger_mismatch. On the forward
	-- path the trigger is NULL, so a genuine first begin proceeds below.
	IF v_trigger IS NOT NULL THEN
		IF NOT (v_trigger <=> arg_operation_id) THEN
			SELECT JSON_OBJECT('outcome', 'trigger_mismatch') AS result;
			LEAVE proc;
		END IF;
		-- Replay returns the DURABLE terminal_reason so a terminal (5/7) replay renders from durable state.
		SELECT JSON_OBJECT('outcome', 'already_begun', 'state', CAST(v_state AS SIGNED), 'terminal_reason', v_term_reason) AS result;
		LEAVE proc;
	END IF;

	-- First call (forward): identify + verify the durable triggering operation.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `status`, `operation_id`
		INTO v_op_status, v_op_id
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END;

	IF v_op_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'operation_not_found') AS result;
		LEAVE proc;
	END IF;
	IF NOT (v_op_id <=> arg_operation_id) THEN
		SELECT JSON_OBJECT('outcome', 'operation_conflict') AS result;
		LEAVE proc;
	END IF;
	-- A settled (succeeded) operation did not fail and must not drive reversal.
	IF v_op_status <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'operation_not_failed', 'status', CAST(v_op_status AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Fence: must hold the lease on a FORWARD workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;

	SET v_event_seq = v_event_seq + 1;

	IF v_top_seq IS NULL THEN
		-- Nothing to compensate -> terminal FAILED (definite failure with NO completed
		-- unwind), lease cleared. Durable state = failed(7), terminal_reason = the reason;
		-- the client renders {failed, compensated:false}. NOT 'reversed' on this path.
		UPDATE `tb_mf_workflow`
		SET `state` = 7,
		    `execution_direction` = 2,
		    `current_disposition` = 2,
		    `continuation` = JSON_OBJECT('pos', 'failed'),
		    `reversal_trigger_operation_id` = arg_operation_id,
		    `terminal_reason` = arg_reason,
		    `lease_owner` = NULL,
		    `lease_expires_at` = NULL,
		    `current_event_seq` = v_event_seq,
		    `current_event_ts` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, v_event_seq, arg_event_ts, 'failed', arg_executor, NULL,
			JSON_OBJECT('reason', arg_reason, 'compensated', 0,
				'operation_seq', arg_operation_seq, 'operation_id', LOWER(HEX(arg_operation_id)))
		);

		SELECT JSON_OBJECT('outcome', 'failed', 'terminal_reason', arg_reason) AS result;
		LEAVE proc;
	END IF;

	-- Compensate the stack -> reversing, lease RETAINED, cursor at the top.
	UPDATE `tb_mf_workflow`
	SET `state` = 2,
	    `execution_direction` = 2,
	    `current_disposition` = 2,
	    `continuation` = JSON_OBJECT('pos', 'reverse', 'seq', v_top_seq),
	    `reversal_trigger_operation_id` = arg_operation_id,
	    `terminal_reason` = arg_reason,
	    `current_event_seq` = v_event_seq,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, v_event_seq, arg_event_ts, 'reversal_begun', arg_executor, NULL,
		JSON_OBJECT('reason', arg_reason, 'top_seq', v_top_seq,
			'operation_seq', arg_operation_seq, 'operation_id', LOWER(HEX(arg_operation_id)))
	);

	SELECT JSON_OBJECT('outcome', 'reversing', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
END $$
DELIMITER ;
