DELIMITER $$
-- Composition (1b.1): reverse a workflow-call checkpoint as a NO-OP (§5 "no-comp reversal").
-- A call checkpoint (its operation's call_kind = child_workflow) NEVER goes through the ordinary
-- reverse_request -> dispatch -> reverse_settle flow, because nothing is ever dispatched for it —
-- 1a build-rejects `compensation`, so no compensation binding can ever exist for it, and
-- reverse_settle hard-requires a persisted reverse_invocation_id that can therefore never be set.
-- This procedure performs the SAME reversal_state 1->2 transition + descend-or-terminal logic as
-- sp_mf_checkpoint_reverse_settle, WITHOUT ever requiring/touching reverse_invocation_id or any
-- of the reverse_* binding columns — they stay NULL throughout, satisfying
-- ck_mf_checkpoint_reverse_binding's existing all-NULL branch (a legitimate "never dispatched,
-- still reversed" case that constraint already allows for).
--
-- Defensively guarded: only reachable for a checkpoint whose OWN operation is call_kind=2
-- (child_workflow) — a participant checkpoint (call_kind=1) is rejected outright
-- (not_call_checkpoint), since it must go through the real reverse_request/dispatch/reverse_settle
-- flow instead. Fenced on REVERSING(2), same as reverse_settle; reverse-order enforced (only the
-- current top active checkpoint may settle); already_reversed is idempotent and checked
-- BEFORE the fence (lease-independent), matching reverse_settle's own "a lost-ack retry must
-- still resolve even with no live token" rationale.
CREATE PROCEDURE `sp_mf_checkpoint_reverse_noop`(
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
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_term_reason varchar(190) DEFAULT NULL;
	DECLARE v_cp_state tinyint;
	DECLARE v_call_kind tinyint;
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_next_seq int DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_cp_missing tinyint(1) DEFAULT 0;
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
	IF arg_seq IS NULL OR arg_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfSeqInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_seq`, `current_event_ts`, `terminal_reason`
		INTO v_owner, v_token, v_state, v_event_seq, v_event_ts, v_term_reason
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

	-- Fence: lease holder on a REVERSING workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Reverse ORDER: only the current top active checkpoint may settle.
	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF arg_seq <> v_top_seq THEN
		SELECT JSON_OBJECT('outcome', 'out_of_order', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Time discipline.
	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	-- Commit the no-op reversal: reversal_state 1->2, reverse_* binding columns untouched (stay
	-- NULL throughout — never dispatched, never bound).
	UPDATE `tb_mf_workflow_checkpoint`
	SET `reversal_state` = 2,
	    `reversed_at` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

	SELECT MAX(`seq`) INTO v_next_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;

	SET v_event_seq = v_event_seq + 1;

	IF v_next_seq IS NULL THEN
		-- Whole stack compensated -> terminal reversed(5), lease cleared.
		UPDATE `tb_mf_workflow`
		SET `state` = 5,
		    `continuation` = JSON_OBJECT('pos', 'reversed'),
		    `lease_owner` = NULL,
		    `lease_expires_at` = NULL,
		    `current_event_seq` = v_event_seq,
		    `current_event_ts` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, v_event_seq, arg_event_ts, 'call_checkpoint_noop_reversed', arg_executor, NULL,
			JSON_OBJECT('seq', arg_seq, 'terminal', 'reversed')
		);

		SELECT JSON_OBJECT('outcome', 'reversed', 'terminal_reason', v_term_reason) AS result;
		LEAVE proc;
	END IF;

	-- More to compensate -> descend, stay reversing, lease RETAINED.
	UPDATE `tb_mf_workflow`
	SET `continuation` = JSON_OBJECT('pos', 'reverse', 'seq', v_next_seq),
	    `current_event_seq` = v_event_seq,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, v_event_seq, arg_event_ts, 'call_checkpoint_noop_reversed', arg_executor, NULL,
		JSON_OBJECT('seq', arg_seq, 'next_seq', v_next_seq)
	);

	SELECT JSON_OBJECT('outcome', 'reversing', 'next_seq', CAST(v_next_seq AS SIGNED)) AS result;
END $$
DELIMITER ;
