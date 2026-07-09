DELIMITER $$
-- Advance the durable reconcile budget for a persistent route-404 on a REVERSE
-- (compensation) dispatch (#2, reverse side), and either DEFER (within budget) or
-- BLOCK (exhausted) — atomically + fence-guarded. Called ONLY on a confirmed
-- Route404 of the compensation PUT/GET.
--
-- Budget lives on the checkpoint row (tb_mf_workflow_checkpoint), keyed
-- (workflow_id, seq) alongside the reverse binding; resume re-dispatches the same
-- checkpoint -> same row -> the budget never resets. Same exhaustion rule as the
-- forward SP: elapsed >= arg_max_elapsed_ms AND attempts >= arg_min_attempts.
--
-- Within budget -> clear lease + next_attempt + 'participant_route_404' warn;
-- state stays reversing(2). Exhausted -> the existing reverse-block semantics
-- (sp_mf_checkpoint_reverse_block, disposition=4 "indeterminate after reconcile
-- exhaustion"): checkpoint resolution_required(3), workflow blocked_resolution(3)
-- RETAINING reverse direction, the reason carried in `continuation`
-- {pos:blocked,seq,direction:reverse,reason}. NO state collapse to forward.
CREATE PROCEDURE `sp_mf_checkpoint_reconcile_defer`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_seq int,
	IN arg_reverse_id varbinary(16),
	IN arg_db_now datetime(6),
	IN arg_next_attempt_at datetime(6),
	IN arg_event_ts datetime(6),
	IN arg_max_elapsed_ms bigint,
	IN arg_min_attempts int
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_cp_state tinyint;
	DECLARE v_cp_revid varbinary(16);
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_attempts int;
	DECLARE v_first_seen datetime(6);
	DECLARE v_elapsed_ms bigint;
	DECLARE v_new_attempts int;
	DECLARE v_new_event_ts datetime(6);
	DECLARE v_append tinyint(1) DEFAULT 1;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_cp_missing tinyint(1) DEFAULT 0;

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
	IF arg_reverse_id IS NULL OR LENGTH(arg_reverse_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReverseIdInvalid';
	END IF;
	IF arg_db_now IS NULL OR arg_next_attempt_at IS NULL OR arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfTimestampInvalid';
	END IF;
	IF arg_max_elapsed_ms IS NULL OR arg_max_elapsed_ms < 0 OR arg_min_attempts IS NULL OR arg_min_attempts < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfBudgetInvalid';
	END IF;
	IF arg_next_attempt_at <= arg_db_now THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfNextAttemptNotFuture';
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

	-- Load the budget-bearing checkpoint + its reverse binding (mirrors sp_mf_checkpoint_reverse_block
	-- ordering: identity + idempotent replay are LEASE-INDEPENDENT, checked before the fence).
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_cp_missing = 1;
		SELECT `reversal_state`, `reverse_invocation_id`, `reconcile_attempts`, `reconcile_first_seen_at`
		INTO v_cp_state, v_cp_revid, v_attempts, v_first_seen
		FROM `tb_mf_workflow_checkpoint`
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq
		FOR UPDATE;
	END;
	IF v_cp_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_found') AS result;
		LEAVE proc;
	END IF;

	-- IDENTITY before replay (lease-independent): the budget binds to a DISPATCHED compensation
	-- (reverse_invocation_id persisted) and the SAME reverse invocation.
	IF v_cp_revid IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'not_requested') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_revid <> arg_reverse_id THEN
		SELECT JSON_OBJECT('outcome', 'reverse_id_mismatch') AS result;
		LEAVE proc;
	END IF;
	-- Idempotent replay (lease-independent — the block cleared the lease): a committed block returns
	-- already_blocked, never fence_lost.
	IF v_cp_state = 3 THEN
		SELECT JSON_OBJECT('outcome', 'already_blocked') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_active', 'reversal_state', CAST(v_cp_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Fence: must hold the lease on a REVERSING(2) workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Only the TOP active checkpoint compensates (mirrors reverse_block): a route-404 on a non-top
	-- checkpoint is out_of_order.
	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF arg_seq <> v_top_seq THEN
		SELECT JSON_OBJECT('outcome', 'out_of_order', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	SET v_new_attempts = v_attempts + 1;
	IF v_first_seen IS NULL THEN SET v_first_seen = arg_db_now; END IF;
	SET v_elapsed_ms = TIMESTAMPDIFF(MICROSECOND, v_first_seen, arg_db_now) DIV 1000;

	-- A BLOCK commits an event -> needs a strictly-advancing event_ts (mirrors reverse_block). On skew,
	-- defer the block to a proper-time retry BEFORE any commit.
	IF v_elapsed_ms >= arg_max_elapsed_ms AND v_new_attempts >= arg_min_attempts AND arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	UPDATE `tb_mf_workflow_checkpoint`
	SET `reconcile_attempts` = v_new_attempts,
	    `reconcile_first_seen_at` = v_first_seen,
	    `reconcile_last_seen_at` = arg_db_now,
	    `reconcile_reason` = 'participant_route_404',
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

	IF v_elapsed_ms >= arg_max_elapsed_ms AND v_new_attempts >= arg_min_attempts THEN
		-- EXHAUSTED -> reverse block (disposition indeterminate=4). Checkpoint resolution_required(3),
		-- workflow blocked_resolution(3) RETAINING reverse direction; reason in the continuation.
		UPDATE `tb_mf_workflow_checkpoint`
		SET `reversal_state` = 3,
		    `resolution_event_ts` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

		UPDATE `tb_mf_workflow`
		SET `state` = 3,
		    `current_disposition` = 4,
		    `continuation` = JSON_OBJECT('pos', 'blocked', 'seq', CAST(arg_seq AS SIGNED), 'direction', 'reverse', 'reason', 'participant_route_unknown'),
		    `lease_owner` = NULL,
		    `lease_expires_at` = NULL,
		    `current_event_ts` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, arg_event_ts, 'participant_route_unknown', arg_executor, NULL,
			JSON_OBJECT('reason', 'participant_route_unknown', 'seq', arg_seq, 'direction', 'reverse', 'disposition', 4,
				'attempts', v_new_attempts, 'elapsed_ms', v_elapsed_ms)
		);

		SELECT JSON_OBJECT('outcome', 'blocked', 'direction', 'reverse', 'reason', 'participant_route_unknown') AS result;
		LEAVE proc;
	END IF;

	-- WITHIN BUDGET -> defer (state stays reversing(2)). Per-attempt warn unless clock skew.
	IF arg_event_ts <= v_event_ts THEN SET v_append = 0; END IF;
	SET v_new_event_ts = v_event_ts;
	IF v_append = 1 THEN
		SET v_new_event_ts = arg_event_ts;
	END IF;

	UPDATE `tb_mf_workflow`
	SET `lease_owner` = NULL,
	    `lease_expires_at` = NULL,
	    `next_attempt_at` = arg_next_attempt_at,
	    `current_event_ts` = v_new_event_ts,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id;

	IF v_append = 1 THEN
		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, arg_event_ts, 'participant_route_404', arg_executor, NULL,
			JSON_OBJECT('reason', 'participant_route_404', 'seq', arg_seq, 'direction', 'reverse',
				'attempt', v_new_attempts, 'elapsed_ms', v_elapsed_ms)
		);
	END IF;

	SELECT JSON_OBJECT('outcome', 'deferred', 'attempts', CAST(v_new_attempts AS SIGNED), 'appended', v_append) AS result;
END $$
DELIMITER ;
