DELIMITER $$
-- Composition (1c): settle a workflow-call checkpoint once its child's OWN compensation has
-- actually reached a terminal compensated state (work/workflow-composition/1c-design.md §2). Called
-- from the parent's reverse loop after sp_mf_checkpoint_reverse_child_reopen ("T1") and after
-- polling the child via sp_mf_call_inspect.
--
-- This SP does NOT trust the caller's own (separate, earlier-transaction) call_inspect read as
-- proof the child is done -- it independently locks and re-reads the child's CURRENT state inside
-- this same transaction and REQUIRES reversed(5) or resolved_exception(6) before flipping the
-- parent's checkpoint (checked as an explicit `IN (5,6)`, not by enumerating every OTHER state --
-- an earlier enumerated-rejection form let forward(1) fall through unnoticed and settle the parent
-- as if compensated; review-caught, fixed). A caller that invokes this while the child is still
-- reversing(2) or blocked_resolution(3) gets child_not_terminal (a normal "not yet" outcome, not an
-- error); any other state -- forward(1) (structurally impossible: a call checkpoint's child is
-- always completed(4) at checkpoint-creation time and reopen only ever transitions
-- completed(4)->reversing(2)), completed(4) (T1 never ran against this child), or the corruption
-- case failed(7) -- gets child_not_compensated (diagnostic -- never silently settled as if
-- compensated).
--
-- Once the child-state precondition is satisfied, the actual checkpoint mechanics (flip
-- reversal_state 1->2, descend to the next active checkpoint or reach the parent's own
-- reversed(5) terminal) are unchanged from what sp_mf_checkpoint_reverse_noop used to do -- that
-- part was never wrong, only its name (and the missing child-state check) were. noop is retired;
-- this is its replacement, not a rename.
CREATE PROCEDURE `sp_mf_checkpoint_reverse_child_settle`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_seq int,
	IN arg_event_ts datetime(6)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_term_reason varchar(190) DEFAULT NULL;
	DECLARE v_cp_state tinyint;
	DECLARE v_call_kind tinyint DEFAULT 1;
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_next_seq int DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_cp_missing tinyint(1) DEFAULT 0;
	DECLARE v_op_missing tinyint(1) DEFAULT 0;

	DECLARE v_child_id varbinary(16);
	DECLARE v_call_missing tinyint(1) DEFAULT 0;
	DECLARE v_child_missing tinyint(1) DEFAULT 0;
	DECLARE v_child_state tinyint;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_executor IS NULL OR LENGTH(arg_executor) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfExecutorInvalid';
	END IF;
	IF arg_fencing_token IS NULL OR arg_fencing_token < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfFencingTokenInvalid';
	END IF;
	IF arg_seq IS NULL OR arg_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfSeqInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_ts`, `terminal_reason`
		INTO v_owner, v_token, v_state, v_event_ts, v_term_reason
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_cp_missing = 1;
		SELECT `reversal_state`
		INTO v_cp_state
		FROM `tb_mf_workflow_checkpoint`
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq
		FOR UPDATE;
	END;

	IF v_cp_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_found') AS result;
		LEAVE proc;
	END IF;

	-- Type guard BEFORE the state machine (structural, never changes over time): this procedure
	-- must never be reachable for a participant checkpoint.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `call_kind` INTO v_call_kind
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_seq;
	END;
	IF v_op_missing = 1 OR v_call_kind <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'not_call_checkpoint') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent lost-ack retry (lease-independent), BEFORE the fence.
	IF v_cp_state = 2 THEN
		SELECT JSON_OBJECT('outcome', 'already_reversed') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_settleable', 'reversal_state', CAST(v_cp_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Fence: lease holder on a REVERSING(2) PARENT.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Reverse ORDER: only the current top active checkpoint may settle. Safe against the
	-- NULL-top-seq case here because the idempotent check above already confirmed arg_seq's own
	-- checkpoint IS reversal_state=1, so it always qualifies as at least one candidate.
	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF arg_seq <> v_top_seq THEN
		SELECT JSON_OBJECT('outcome', 'out_of_order', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- THE precondition this SP exists to enforce (review finding #1): independently verify, in
	-- this same transaction, that the child's OWN compensation actually reached a terminal
	-- compensated state -- never trust the caller's separate, earlier call_inspect read.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_call_missing = 1;
		SELECT `child_workflow_id` INTO v_child_id
		FROM `tb_mf_call`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_seq;
	END;
	IF v_call_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'call_sidecar_not_found') AS result;
		LEAVE proc;
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_child_missing = 1;
		SELECT `state` INTO v_child_state
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = v_child_id
		FOR UPDATE;
	END;
	IF v_child_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'child_not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_child_state IN (2, 3) THEN
		-- Genuinely still in flight (reversing) or stuck (blocked_resolution) -- the EXPECTED
		-- "not yet" shape, not an error. The caller should defer and re-poll.
		SELECT JSON_OBJECT('outcome', 'child_not_terminal', 'child_state', CAST(v_child_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;
	IF v_child_state NOT IN (5, 6) THEN
		-- Require (5,6) EXPLICITLY rather than enumerating the rejected states: state=1 (forward)
		-- should be structurally impossible here (a call checkpoint's child was always
		-- completed(4) at checkpoint-creation time, and reopen only ever transitions
		-- completed(4)->reversing(2) -- forward(1) means the child was mutated through some path
		-- other than T1, or the checkpoint/child pairing is corrupt); state=4 means T1 was never
		-- run against this child; state=7 is the same corruption case documented in
		-- sp_mf_checkpoint_reverse_child_reopen. All diagnostic, never a normal polling outcome --
		-- an earlier enumerated (4,7) form here let state=1 fall through and settle the parent's
		-- checkpoint as if the child had actually compensated (review-caught, fixed).
		SELECT JSON_OBJECT('outcome', 'child_not_compensated', 'child_state', CAST(v_child_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;
	-- Only reversed(5) / resolved_exception(6) reach here. This procedure's own CONTROL FLOW never
	-- branches on which -- both mean "the child is done reversing" and settle identically, since the
	-- parent must never enumerate or drive the child's own internal compensation outcome. The
	-- compensation_settled audit event below DOES record child_state (5 or 6) as an observability
	-- correlation field (1c-design.md's own requirement) -- that is passive audit trail, not a
	-- decision the parent's logic makes differently, so it does not violate the invariant above.

	-- Time discipline (this SP does not write the child's own event stream, only read it above).
	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	-- Commit the settle: reversal_state 1->2, descend-or-terminal. Unchanged mechanics from the
	-- retired sp_mf_checkpoint_reverse_noop.
	UPDATE `tb_mf_workflow_checkpoint`
	SET `reversal_state` = 2,
	    `reversed_at` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

	SELECT MAX(`seq`) INTO v_next_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF v_next_seq IS NULL THEN
		-- Whole stack compensated -> terminal reversed(5), lease cleared.
		UPDATE `tb_mf_workflow`
		SET `state` = 5,
		    `continuation` = JSON_OBJECT('pos', 'reversed'),
		    `lease_owner` = NULL,
		    `lease_expires_at` = NULL,
		    `current_event_ts` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, arg_event_ts, 'compensation_settled', arg_executor, NULL,
			JSON_OBJECT('seq', arg_seq, 'terminal', 'reversed',
				'child_workflow_id', LOWER(HEX(v_child_id)), 'child_state', CAST(v_child_state AS SIGNED))
		);

		SELECT JSON_OBJECT('outcome', 'reversed', 'terminal_reason', v_term_reason) AS result;
		LEAVE proc;
	END IF;

	-- More to compensate -> descend, stay reversing, lease RETAINED.
	UPDATE `tb_mf_workflow`
	SET `continuation` = JSON_OBJECT('pos', 'reverse', 'seq', v_next_seq),
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, arg_event_ts, 'compensation_settled', arg_executor, NULL,
		JSON_OBJECT('seq', arg_seq, 'next_seq', v_next_seq,
			'child_workflow_id', LOWER(HEX(v_child_id)), 'child_state', CAST(v_child_state AS SIGNED))
	);

	SELECT JSON_OBJECT('outcome', 'reversing', 'next_seq', CAST(v_next_seq AS SIGNED)) AS result;
END $$
DELIMITER ;
