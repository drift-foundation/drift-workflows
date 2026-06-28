DELIMITER $$
-- Advance the durable reconcile budget for a persistent FORWARD route-404 (#2),
-- and either DEFER (still within budget) or BLOCK (exhausted) — atomically and
-- fence-guarded. Called ONLY on a confirmed Route404 (participant has no record
-- AND won't accept the PUT), never on 202/5xx/transport.
--
-- Budget lives on the operation row (tb_mf_operation), keyed (workflow_id,
-- operation_seq): a resume re-dispatches the same seq -> same row -> the count +
-- first_seen persist, so the budget can never be reset by retry/resume.
--
-- Exhaustion rule: elapsed >= arg_max_elapsed_ms AND attempts >= arg_min_attempts
-- (wall-time is the only real bound; min_attempts is a small floor so one 404 +
-- clock skew can't block). No max-attempts cap -> attempts can never win early.
--
-- Within budget -> clear lease + set next_attempt + a per-attempt warn event
-- 'participant_route_404'; state stays forward(1), op stays requested.
-- Exhausted -> forward(1) -> blocked_resolution(3), direction forward(1),
-- disposition indeterminate(4) (the op NEVER executed -> uncertain, not failed),
-- lease cleared, the durable reason carried in `continuation`
-- {pos:blocked,direction:forward,reason,operation_seq,operation_id} (so inspect
-- replay renders it), event 'participant_route_unknown'. NO compensation; prior
-- checkpoints are untouched; the op stays requested for operator resolution.
CREATE PROCEDURE `sp_mf_workflow_reconcile_defer`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
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
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_op_id varbinary(16);
	DECLARE v_op_status tinyint;
	DECLARE v_attempts int;
	DECLARE v_first_seen datetime(6);
	DECLARE v_elapsed_ms bigint;
	DECLARE v_new_attempts int;
	DECLARE v_new_event_seq bigint;
	DECLARE v_new_event_ts datetime(6);
	DECLARE v_append tinyint(1) DEFAULT 1;
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
	IF arg_db_now IS NULL OR arg_next_attempt_at IS NULL OR arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfTimestampInvalid';
	END IF;
	IF arg_max_elapsed_ms IS NULL OR arg_max_elapsed_ms < 0 OR arg_min_attempts IS NULL OR arg_min_attempts < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfBudgetInvalid';
	END IF;
	-- A within-budget defer is a FUTURE backoff (mirrors sp_mf_operation_dispatch_defer): reject a
	-- past/immediate next_attempt that would hot-spin. (Ignored on the exhaustion path.)
	IF arg_next_attempt_at <= arg_db_now THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfNextAttemptNotFuture';
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

	-- Fence: must hold the lease on a FORWARD(1) workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Load + verify the budget-bearing operation row.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `operation_id`, `status`, `reconcile_attempts`, `reconcile_first_seen_at`
		INTO v_op_id, v_op_status, v_attempts, v_first_seen
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq
		FOR UPDATE;
	END;
	IF v_op_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'operation_not_found') AS result;
		LEAVE proc;
	END IF;
	IF NOT (v_op_id <=> arg_operation_id) THEN
		SELECT JSON_OBJECT('outcome', 'operation_conflict') AS result;
		LEAVE proc;
	END IF;
	-- The budget applies to an IN-FLIGHT route-404. A settled (succeeded) op never route-404s -> a bad
	-- caller must not advance/block a settled row.
	IF v_op_status <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'operation_not_requested', 'status', CAST(v_op_status AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	SET v_new_attempts = v_attempts + 1;
	IF v_first_seen IS NULL THEN SET v_first_seen = arg_db_now; END IF;
	SET v_elapsed_ms = TIMESTAMPDIFF(MICROSECOND, v_first_seen, arg_db_now) DIV 1000;

	-- A BLOCK commits an event, so it needs a strictly-advancing event_ts (mirrors reverse_block /
	-- operation_fail). On skew, defer the block to a proper-time retry BEFORE any commit.
	IF v_elapsed_ms >= arg_max_elapsed_ms AND v_new_attempts >= arg_min_attempts AND arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	-- Persist the advanced budget on the op row (first_seen anchored once; op stays requested).
	UPDATE `tb_mf_operation`
	SET `reconcile_attempts` = v_new_attempts,
	    `reconcile_first_seen_at` = v_first_seen,
	    `reconcile_last_seen_at` = arg_db_now,
	    `reconcile_reason` = 'participant_route_404',
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;

	IF v_elapsed_ms >= arg_max_elapsed_ms AND v_new_attempts >= arg_min_attempts THEN
		-- EXHAUSTED -> durable BLOCK. The reason lives in the continuation so inspect replay renders it.
		SET v_new_event_seq = v_event_seq + 1;
		UPDATE `tb_mf_workflow`
		SET `state` = 3,
		    `execution_direction` = 1,
		    `current_disposition` = 4,
		    `continuation` = JSON_OBJECT('pos', 'blocked', 'direction', 'forward',
		        'reason', 'participant_route_unknown', 'operation_seq', CAST(arg_operation_seq AS SIGNED),
		        'operation_id', LOWER(HEX(arg_operation_id))),
		    `lease_owner` = NULL,
		    `lease_expires_at` = NULL,
		    `current_event_seq` = v_new_event_seq,
		    `current_event_ts` = arg_event_ts,
		    `updated_at` = arg_db_now
		WHERE `workflow_id` = arg_workflow_id;

		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, v_new_event_seq, arg_event_ts, 'participant_route_unknown', arg_executor, NULL,
			JSON_OBJECT('reason', 'participant_route_unknown', 'operation_seq', arg_operation_seq,
				'operation_id', LOWER(HEX(arg_operation_id)),
				'attempts', v_new_attempts, 'elapsed_ms', v_elapsed_ms)
		);

		SELECT JSON_OBJECT('outcome', 'blocked', 'direction', 'forward', 'reason', 'participant_route_unknown') AS result;
		LEAVE proc;
	END IF;

	-- WITHIN BUDGET -> defer. Per-attempt 'participant_route_404' warn event, unless a non-increasing
	-- event_ts (clock skew) can't order it -> fold into no-append; the defer still proceeds.
	IF arg_event_ts <= v_event_ts THEN SET v_append = 0; END IF;
	SET v_new_event_seq = v_event_seq;
	SET v_new_event_ts = v_event_ts;
	IF v_append = 1 THEN
		SET v_new_event_seq = v_event_seq + 1;
		SET v_new_event_ts = arg_event_ts;
	END IF;

	UPDATE `tb_mf_workflow`
	SET `lease_owner` = NULL,
	    `lease_expires_at` = NULL,
	    `next_attempt_at` = arg_next_attempt_at,
	    `current_event_seq` = v_new_event_seq,
	    `current_event_ts` = v_new_event_ts,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id;

	IF v_append = 1 THEN
		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, v_new_event_seq, arg_event_ts, 'participant_route_404', arg_executor, NULL,
			JSON_OBJECT('reason', 'participant_route_404', 'operation_seq', arg_operation_seq,
				'operation_id', LOWER(HEX(arg_operation_id)),
				'attempt', v_new_attempts, 'elapsed_ms', v_elapsed_ms)
		);
	END IF;

	SELECT JSON_OBJECT('outcome', 'deferred', 'attempts', CAST(v_new_attempts AS SIGNED), 'appended', v_append) AS result;
END $$
DELIMITER ;
