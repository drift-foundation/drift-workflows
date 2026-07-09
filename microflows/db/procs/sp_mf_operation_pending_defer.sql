DELIMITER $$
-- Advance the durable pending->re-dispatch timer for a FORWARD operation whose
-- participant CONFIRMED it is still pending (GET 202) on a RECOVERED dispatch
-- (Phase 7 case [12]), and either DEFER (still within the interval) or REDISPATCH
-- (interval elapsed -> the runner is about to re-PUT under the held lease) —
-- atomically + fence-guarded. Called ONLY on a confirmed PendingObserved of a
-- recovered op, NEVER on a fresh dispatch, a 5xx/transport (PendingUncertain), or
-- a 404 (that is the #2 reconcile budget).
--
-- The timer lives on the operation row (tb_mf_operation), keyed (workflow_id,
-- operation_seq): a resume re-dispatches the same seq -> same row -> first_seen +
-- count persist, so the escalation epoch can never be reset by retry/resume.
--
-- Escalation rule: elapsed = db_now - COALESCE(redispatch_last_at,
-- redispatch_first_seen_at). elapsed >= arg_after_ms -> REDISPATCH; else DEFER.
-- Unlike the #2 budget there is NO exhaustion/block: a re-PUT is idempotent, so
-- this escalates indefinitely. first_seen is anchored ONCE (the epoch); last_at +
-- count advance ONLY on a redispatch (each re-arms the next interval).
--
-- DEFER (within interval) -> in ONE fenced txn: anchor first_seen (if NULL) + clear
-- lease + set next_attempt_at + append a 'pending_deferred' event. The runner must
-- NOT separately _defer_pending after this; the release already happened atomically
-- (a crash between the timer update and the release is impossible). op stays
-- requested, state stays forward(1).
-- REDISPATCH (interval elapsed) -> anchor first_seen (if NULL) + advance the timer
-- (last_at = db_now, count += 1) + append a 'pending_redispatch' event, and KEEP the
-- lease (the runner re-PUTs the byte-identical operation under the held lease). No
-- settle/checkpoint; op stays requested, state stays forward(1).
CREATE PROCEDURE `sp_mf_operation_pending_defer`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
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
	DECLARE v_op_id varbinary(16);
	DECLARE v_op_status tinyint;
	DECLARE v_count int;
	DECLARE v_first_seen datetime(6);
	DECLARE v_last_at datetime(6);
	DECLARE v_anchor datetime(6);
	DECLARE v_elapsed_ms bigint;
	DECLARE v_new_count int;
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
	IF arg_after_ms IS NULL OR arg_after_ms < 0 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfPendingAfterInvalid';
	END IF;
	-- A within-interval defer is a FUTURE backoff (mirrors sp_mf_workflow_reconcile_defer): reject a
	-- past/immediate next_attempt that would hot-spin. (Unused on the redispatch path; the lease is held.)
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

	-- Fence: must hold the lease on a FORWARD(1) workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Load + verify the timer-bearing operation row.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `operation_id`, `status`, `redispatch_count`, `redispatch_first_seen_at`, `redispatch_last_at`
		INTO v_op_id, v_op_status, v_count, v_first_seen, v_last_at
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
	-- The timer applies to an IN-FLIGHT pending op. A settled (succeeded) op never pends -> a bad caller
	-- must not escalate a settled row.
	IF v_op_status <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'operation_not_requested', 'status', CAST(v_op_status AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Anchor the epoch once; elapsed measured from the last re-arm (or the epoch if never re-armed).
	IF v_first_seen IS NULL THEN SET v_first_seen = arg_db_now; END IF;
	SET v_anchor = COALESCE(v_last_at, v_first_seen);
	SET v_elapsed_ms = TIMESTAMPDIFF(MICROSECOND, v_anchor, arg_db_now) DIV 1000;

	-- An event needs a strictly-advancing event_ts to order it (mirrors reconcile_defer's within-budget
	-- defer). On skew, fold to no-append; the defer/redispatch still proceeds (neither is a terminal commit).
	IF arg_event_ts <= v_event_ts THEN SET v_append = 0; END IF;
	SET v_new_event_ts = v_event_ts;
	IF v_append = 1 THEN
		SET v_new_event_ts = arg_event_ts;
	END IF;

	IF v_elapsed_ms >= arg_after_ms THEN
		-- REDISPATCH -> advance the timer + re-arm; KEEP the lease (the runner is about to re-PUT).
		SET v_new_count = v_count + 1;
		UPDATE `tb_mf_operation`
		SET `redispatch_first_seen_at` = v_first_seen,
		    `redispatch_last_at` = arg_db_now,
		    `redispatch_count` = v_new_count,
		    `updated_at` = arg_db_now
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;

		IF v_append = 1 THEN
			UPDATE `tb_mf_workflow`
			SET `current_event_ts` = v_new_event_ts,
			    `updated_at` = arg_db_now
			WHERE `workflow_id` = arg_workflow_id;
			INSERT INTO `tb_mf_workflow_event` (
				`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
			) VALUES (
				arg_workflow_id, arg_event_ts, 'pending_redispatch', arg_executor, NULL,
				JSON_OBJECT('reason', 'pending_redispatch', 'operation_seq', arg_operation_seq,
					'operation_id', LOWER(HEX(arg_operation_id)),
					'redispatch_count', v_new_count, 'elapsed_ms', v_elapsed_ms)
			);
		END IF;

		SELECT JSON_OBJECT('outcome', 'redispatch', 'redispatch_count', CAST(v_new_count AS SIGNED), 'appended', CAST(v_append AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- WITHIN INTERVAL -> defer: anchor the epoch (count/last_at untouched so elapsed keeps accumulating),
	-- clear the lease, set next_attempt, append the deferred event — all in this one fenced txn.
	UPDATE `tb_mf_operation`
	SET `redispatch_first_seen_at` = v_first_seen,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;

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
			JSON_OBJECT('reason', 'pending_deferred', 'operation_seq', arg_operation_seq,
				'operation_id', LOWER(HEX(arg_operation_id)),
				'redispatch_count', v_count, 'elapsed_ms', v_elapsed_ms)
		);
	END IF;

	SELECT JSON_OBJECT('outcome', 'defer', 'redispatch_count', CAST(v_count AS SIGNED), 'appended', CAST(v_append AS SIGNED)) AS result;
END $$
DELIMITER ;
