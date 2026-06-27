DELIMITER $$
-- Begin durable reversal on an AUTHORED fail (a `.mf` `fail` node): forward(1) ->
-- reversing(2), direction reverse(2), disposition failed(2), so the committed
-- checkpoint stack is compensated in reverse order.
--
-- UNLIKE sp_mf_workflow_begin_reversal, this has NO "operation failed" precondition:
-- an authored fail is a workflow-policy decision that follows a SETTLED 200 result
-- (the triggering op SUCCEEDED). Reversal here is driven by the .mf `fail` node, not
-- by a definite operation rejection — so this is its OWN durable transition path.
--
-- The command is identified DURABLY by arg_fail_id = H(workflow_id, pinned
-- content_hash, fail node id, domain) — a stable command identity. It is persisted
-- as reversal_trigger_operation_id; a replay with the SAME id returns the durable
-- state/reason, a DIFFERENT id is trigger_mismatch (mirrors begin_reversal safety).
--
-- If there are NO active checkpoints there is nothing to compensate -> terminal
-- failed(7) (compensated:false), lease cleared. Otherwise -> reversing(2), lease
-- RETAINED, continuation = the reverse cursor at the TOP (highest-seq) checkpoint.
--
-- Fenced + time-disciplined. Idempotent: already reversing/failed returns that
-- outcome lease-independently.
CREATE PROCEDURE `sp_mf_workflow_authored_fail`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_fail_id varbinary(16),
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
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
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
	IF arg_fail_id IS NULL OR LENGTH(arg_fail_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfFailIdInvalid';
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

	-- Idempotent replay BOUND to the fail command id (lease-independent across all reverse
	-- states). A different command is trigger_mismatch. On the forward path the trigger is
	-- NULL, so a genuine first authored fail proceeds below.
	IF v_trigger IS NOT NULL THEN
		IF NOT (v_trigger <=> arg_fail_id) THEN
			SELECT JSON_OBJECT('outcome', 'trigger_mismatch') AS result;
			LEAVE proc;
		END IF;
		-- Replay returns the DURABLE terminal_reason so a terminal (5/7) replay renders from durable state.
		SELECT JSON_OBJECT('outcome', 'already_begun', 'state', CAST(v_state AS SIGNED), 'terminal_reason', v_term_reason) AS result;
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
		-- Nothing to compensate -> terminal FAILED(7) (authored failure with NO completed
		-- unwind), lease cleared. Durable state = failed, terminal_reason = the reason; the
		-- client renders {failed, compensated:false}.
		UPDATE `tb_mf_workflow`
		SET `state` = 7,
		    `execution_direction` = 2,
		    `current_disposition` = 2,
		    `continuation` = JSON_OBJECT('pos', 'failed'),
		    `reversal_trigger_operation_id` = arg_fail_id,
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
				'source', 'authored_fail', 'fail_id', LOWER(HEX(arg_fail_id)))
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
	    `reversal_trigger_operation_id` = arg_fail_id,
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
			'source', 'authored_fail', 'fail_id', LOWER(HEX(arg_fail_id)))
	);

	SELECT JSON_OBJECT('outcome', 'reversing', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
END $$
DELIMITER ;
