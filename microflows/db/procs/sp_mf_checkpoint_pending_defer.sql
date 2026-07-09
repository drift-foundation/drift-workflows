DELIMITER $$
-- Advance the durable pending->re-dispatch timer for a REVERSE (compensation)
-- dispatch whose participant CONFIRMED it is still pending on a RECOVERED dispatch
-- (Phase 7 case [12], reverse side), and either DEFER (within interval) or
-- REDISPATCH (interval elapsed -> the runner re-PUTs the pinned compensation under
-- the held lease) — atomically + fence-guarded. Called ONLY on a confirmed
-- PendingObserved of the compensation, never on 5xx/transport/404.
--
-- The timer lives on the checkpoint row (tb_mf_workflow_checkpoint), keyed
-- (workflow_id, seq) alongside the reverse binding; resume re-dispatches the same
-- checkpoint -> same row -> the epoch never resets. Same escalation rule as the
-- forward SP (elapsed = db_now - COALESCE(last_at, first_seen); >= arg_after_ms ->
-- redispatch), and the same NO-exhaustion discipline.
--
-- DEFER -> clear lease + next_attempt + anchor first_seen + 'pending_deferred' warn;
-- state stays reversing(2). REDISPATCH -> advance the timer (last_at, count) +
-- 'pending_redispatch' warn, KEEP the lease; state stays reversing(2).
CREATE PROCEDURE `sp_mf_checkpoint_pending_defer`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_seq int,
	IN arg_reverse_id varbinary(16),
	IN arg_db_now datetime(6),
	IN arg_next_attempt_at datetime(6),
	IN arg_event_ts datetime(6),
	IN arg_after_ms bigint
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_cp_state tinyint;
	DECLARE v_cp_revid varbinary(16);
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_count int;
	DECLARE v_first_seen datetime(6);
	DECLARE v_last_at datetime(6);
	DECLARE v_anchor datetime(6);
	DECLARE v_elapsed_ms bigint;
	DECLARE v_new_count int;
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
	IF arg_after_ms IS NULL OR arg_after_ms < 0 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfPendingAfterInvalid';
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

	-- Load the timer-bearing checkpoint + its reverse binding (mirrors reconcile_defer / reverse_block
	-- ordering: identity + idempotent replay are LEASE-INDEPENDENT, checked before the fence).
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_cp_missing = 1;
		SELECT `reversal_state`, `reverse_invocation_id`, `redispatch_count`, `redispatch_first_seen_at`, `redispatch_last_at`
		INTO v_cp_state, v_cp_revid, v_count, v_first_seen, v_last_at
		FROM `tb_mf_workflow_checkpoint`
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq
		FOR UPDATE;
	END;
	IF v_cp_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_found') AS result;
		LEAVE proc;
	END IF;

	-- IDENTITY before replay (lease-independent): the timer binds to a DISPATCHED compensation
	-- (reverse_invocation_id persisted) and the SAME reverse invocation.
	IF v_cp_revid IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'not_requested') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_revid <> arg_reverse_id THEN
		SELECT JSON_OBJECT('outcome', 'reverse_id_mismatch') AS result;
		LEAVE proc;
	END IF;
	-- Idempotent replay (lease-independent): a checkpoint already blocked for resolution is a no-op.
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

	-- Only the TOP active checkpoint compensates (mirrors reverse_block): a pending on a non-top
	-- checkpoint is out_of_order.
	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF arg_seq <> v_top_seq THEN
		SELECT JSON_OBJECT('outcome', 'out_of_order', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	IF v_first_seen IS NULL THEN SET v_first_seen = arg_db_now; END IF;
	SET v_anchor = COALESCE(v_last_at, v_first_seen);
	SET v_elapsed_ms = TIMESTAMPDIFF(MICROSECOND, v_anchor, arg_db_now) DIV 1000;

	-- Event ordering: fold to no-append on skew (no terminal commit either way).
	IF arg_event_ts <= v_event_ts THEN SET v_append = 0; END IF;
	SET v_new_event_ts = v_event_ts;
	IF v_append = 1 THEN
		SET v_new_event_ts = arg_event_ts;
	END IF;

	IF v_elapsed_ms >= arg_after_ms THEN
		-- REDISPATCH -> advance the timer + re-arm; KEEP the lease (the runner re-PUTs the compensation).
		SET v_new_count = v_count + 1;
		UPDATE `tb_mf_workflow_checkpoint`
		SET `redispatch_first_seen_at` = v_first_seen,
		    `redispatch_last_at` = arg_db_now,
		    `redispatch_count` = v_new_count,
		    `updated_at` = arg_db_now
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

		IF v_append = 1 THEN
			UPDATE `tb_mf_workflow`
			SET `current_event_ts` = v_new_event_ts,
			    `updated_at` = arg_db_now
			WHERE `workflow_id` = arg_workflow_id;
			INSERT INTO `tb_mf_workflow_event` (
				`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
			) VALUES (
				arg_workflow_id, arg_event_ts, 'pending_redispatch', arg_executor, NULL,
				JSON_OBJECT('reason', 'pending_redispatch', 'seq', arg_seq, 'direction', 'reverse',
					'redispatch_count', v_new_count, 'elapsed_ms', v_elapsed_ms)
			);
		END IF;

		SELECT JSON_OBJECT('outcome', 'redispatch', 'redispatch_count', CAST(v_new_count AS SIGNED), 'appended', CAST(v_append AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- WITHIN INTERVAL -> defer (state stays reversing(2)). Anchor the epoch; clear lease + next_attempt.
	UPDATE `tb_mf_workflow_checkpoint`
	SET `redispatch_first_seen_at` = v_first_seen,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

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
			arg_workflow_id, arg_event_ts, 'pending_deferred', arg_executor, NULL,
			JSON_OBJECT('reason', 'pending_deferred', 'seq', arg_seq, 'direction', 'reverse',
				'redispatch_count', v_count, 'elapsed_ms', v_elapsed_ms)
		);
	END IF;

	SELECT JSON_OBJECT('outcome', 'defer', 'redispatch_count', CAST(v_count AS SIGNED), 'appended', CAST(v_append AS SIGNED)) AS result;
END $$
DELIMITER ;
