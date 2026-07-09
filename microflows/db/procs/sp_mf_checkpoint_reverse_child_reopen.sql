DELIMITER $$
-- Composition (1c) — "T1": reopen a workflow-call checkpoint's COMPLETED child into reversal
-- (work/workflow-composition/1c-design.md §1-2). Called from the PARENT's reverse loop when it
-- reaches a call_kind=2 checkpoint; asks the child to compensate itself by flipping
-- completed(4) -> reversing(2), fenced on the PARENT (the child has no lease of its own to fence —
-- see the header note on the write phase below).
--
-- Idempotency is keyed on the CHILD's current state (checked lease-independently, before the
-- parent fence, exactly like every other reverse_* SP's own idempotent-replay check):
--   completed(4)                              -> reopen (the only state that writes anything)
--   reversing/blocked_resolution/reversed/
--     resolved_exception (2,3,5,6)            -> already_reopened (no write; a prior call already
--                                                did this, e.g. crash-retry)
--   failed(7)                                 -> child_state_inconsistent (no write) -- a failed
--                                                child should never have become a parent checkpoint
--                                                at all (DESIGN.md §4); this is corruption evidence,
--                                                not a benign skip, so it is never silently settled.
--
-- This is an explicit TWO-ROW transaction (parent + child), not a child-only write: the parent gets
-- a `compensation_requested` audit event (parity with the participant path's own event of that
-- name) in the SAME commit as the child's reopen. Precedented by sp_mf_call_submit, which already
-- writes parent+child in one transaction. Because two event streams advance together, the time
-- discipline check is against BOTH timelines (arg_event_ts must be strictly after both the
-- parent's and the child's own current_event_ts).
--
-- Lock ordering is always parent-row-then-child-row (this SP and sp_mf_checkpoint_reverse_child_settle
-- agree on this) -- no existing procedure locks a child's own tb_mf_workflow row before its
-- parent's in a way that could conflict (sp_mf_child_terminal_notify deliberately never locks the
-- child's own row in the same transaction as the parent's, for exactly this class of risk).
CREATE PROCEDURE `sp_mf_checkpoint_reverse_child_reopen`(
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
	DECLARE v_parent_event_ts datetime(6);
	DECLARE v_call_kind tinyint DEFAULT 1;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_cp_missing tinyint(1) DEFAULT 0;
	DECLARE v_op_missing tinyint(1) DEFAULT 0;

	DECLARE v_child_id varbinary(16);
	DECLARE v_call_missing tinyint(1) DEFAULT 0;
	DECLARE v_child_missing tinyint(1) DEFAULT 0;
	DECLARE v_child_state tinyint;
	DECLARE v_child_event_ts datetime(6);
	DECLARE v_child_top_seq int DEFAULT NULL;

	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_defer_until datetime(6);
	DECLARE v_dummy int;

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
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_ts`
		INTO v_owner, v_token, v_state, v_parent_event_ts
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
		SELECT 1 INTO v_dummy FROM `tb_mf_workflow_checkpoint`
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

	-- Resolve the child via the sidecar (guaranteed to exist per fk_mf_call_operation once the
	-- type guard above passes; defensive miss-check anyway).
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
		SELECT `state`, `current_event_ts`
		INTO v_child_state, v_child_event_ts
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = v_child_id
		FOR UPDATE;
	END;
	IF v_child_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'child_not_found') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent-replay / diagnostic check on CHILD state (lease-independent, BEFORE the parent
	-- fence -- same ordering every other reverse_* SP's own idempotent check uses).
	IF v_child_state IN (2, 3, 5, 6) THEN
		SELECT JSON_OBJECT('outcome', 'already_reopened', 'child_state', CAST(v_child_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;
	IF v_child_state = 7 THEN
		-- Corruption evidence, not a benign skip (1c-design.md §1): a failed child should never
		-- have become a parent checkpoint. Never settle/reopen as if this were a normal case.
		SELECT JSON_OBJECT('outcome', 'child_state_inconsistent', 'child_state', CAST(v_child_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;
	IF v_child_state <> 4 THEN
		-- Defensive: any other value is not a representable tb_mf_workflow.state at all.
		SELECT JSON_OBJECT('outcome', 'child_state_inconsistent', 'child_state', CAST(v_child_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Fence: lease holder on a REVERSING(2) PARENT.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Reverse ORDER on the PARENT's own stack (NULL-safe: no idempotent checkpoint-state check
	-- precedes this here, unlike settle, so v_top_seq legitimately CAN be NULL if arg_seq's own
	-- checkpoint was already reversed by an earlier, different call).
	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF v_top_seq IS NULL OR arg_seq <> v_top_seq THEN
		SELECT JSON_OBJECT('outcome', 'out_of_order', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- The child's OWN top active checkpoint, for its new reverse cursor. Every completed(4)
	-- workflow settled >= 1 operation and therefore has >= 1 active checkpoint (1c-design.md §1's
	-- invariant) -- NULL here is a durable inconsistency, never a valid trivial-unwind case (unlike
	-- begin_reversal's own NULL-top-seq branch, which is a legitimate forward-failure-with-nothing-
	-- to-compensate case that does not apply to a completed(4) reopen).
	SELECT MAX(`seq`) INTO v_child_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = v_child_id AND `reversal_state` = 1;
	IF v_child_top_seq IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'child_no_active_checkpoint') AS result;
		LEAVE proc;
	END IF;

	-- Time discipline against BOTH timelines: two event streams advance in this one commit.
	IF arg_event_ts <= v_parent_event_ts OR arg_event_ts <= v_child_event_ts THEN
		SET v_defer_until = GREATEST(v_parent_event_ts, v_child_event_ts) + INTERVAL 5 SECOND;
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_defer_until, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	-- ONLY NOW -- every possible rejection already ruled out -- the write phase.

	-- Parent: audit only -- state/continuation are NOT touched (the parent is still "at" this
	-- checkpoint per its existing reverse cursor; only its event trail advances).
	UPDATE `tb_mf_workflow`
	SET `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, arg_event_ts, 'compensation_requested', arg_executor, NULL,
		JSON_OBJECT('seq', arg_seq, 'child_workflow_id', LOWER(HEX(v_child_id)))
	);

	-- Child: the reopen. completed(4) -> reversing(2); disposition failed(2) is the closest
	-- existing fit for "this instance's forward result is being undone" (no third disposition code
	-- exists for "reversing because an ancestor decided to compensate" -- confirmed acceptable on
	-- design review, no new code introduced for MVP). fencing_token bumped as a "direct
	-- intervention" transition, matching tb_mf_workflow.sql's own documented convention, even though
	-- there is no live lease to invalidate (defense-in-depth, not load-bearing). next_attempt_at =
	-- arg_event_ts makes the child immediately claimable -- this write IS the wake; no separate
	-- notify call is needed. workflow_return_json is cleared: ck_mf_workflow_state_return requires
	-- it NULL for every state other than completed(4), and the child is no longer completed.
	UPDATE `tb_mf_workflow`
	SET `state` = 2,
	    `execution_direction` = 2,
	    `current_disposition` = 2,
	    `continuation` = JSON_OBJECT('pos', 'reverse', 'seq', v_child_top_seq),
	    `fencing_token` = `fencing_token` + 1,
	    `terminal_reason` = 'parent_compensation',
	    `workflow_return_json` = NULL,
	    `next_attempt_at` = arg_event_ts,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = v_child_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		v_child_id, arg_event_ts, 'compensation_requested_by_parent', arg_executor, NULL,
		JSON_OBJECT('parent_workflow_id', LOWER(HEX(arg_workflow_id)), 'parent_operation_seq', CAST(arg_seq AS SIGNED))
	);

	SELECT JSON_OBJECT('outcome', 'reopened', 'child_workflow_id', LOWER(HEX(v_child_id))) AS result;
END $$
DELIMITER ;
