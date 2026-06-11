DELIMITER $$
-- Enter blocked_resolution when automatic reversal CANNOT safely continue
-- (microflows_design.md §3.1): a compensation that failed nonretryably
-- (disposition failed=2) or is indeterminate after reconcile exhaustion
-- (indeterminate=4). The checkpoint becomes resolution_required(3) and the
-- workflow blocked_resolution(3) RETAINING the reverse direction, with the lease
-- released and a durable diagnostic event recording the reason + evidence.
--
-- This owns ENTRY into blocked only; authorized administration OUT of blocked is a
-- follow-up. The target must be the current TOP active checkpoint (reverse order).
-- Fenced on REVERSING(2); time-disciplined. already_blocked is checked BEFORE the
-- fence (lease-independent): blocking clears the lease, so a retry must still
-- resolve to already_blocked rather than fence_lost.
CREATE PROCEDURE `sp_mf_checkpoint_reverse_block`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_seq int,
	IN arg_reverse_id varbinary(16),
	IN arg_disposition tinyint,
	IN arg_reason varchar(64),
	IN arg_event_ts datetime(6)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_cp_state tinyint;
	DECLARE v_cp_revid varbinary(16);
	DECLARE v_top_seq int DEFAULT NULL;
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
	-- Only failure(2) or indeterminate(4) may block during reverse.
	IF arg_disposition IS NULL OR arg_disposition NOT IN (2, 4) THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfDispositionInvalid';
	END IF;
	IF arg_reason IS NULL OR arg_reason = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReasonInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
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

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_cp_missing = 1;
		SELECT `reversal_state`, `reverse_invocation_id`
		INTO v_cp_state, v_cp_revid
		FROM `tb_mf_workflow_checkpoint`
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq
		FOR UPDATE;
	END;

	IF v_cp_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_found') AS result;
		LEAVE proc;
	END IF;

	-- IDENTITY before replay: blocking is only valid for a compensation that was
	-- actually DISPATCHED (reverse_invocation_id persisted) and definitively
	-- failed/indeterminate — never a checkpoint with no recorded dispatch. Ties the
	-- block to the same reverse invocation. Lease-independent (read-only).
	IF v_cp_revid IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'not_requested') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_revid <> arg_reverse_id THEN
		SELECT JSON_OBJECT('outcome', 'reverse_id_mismatch') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent (lease-independent — blocking cleared the lease), AFTER identity.
	IF v_cp_state = 3 THEN
		SELECT JSON_OBJECT('outcome', 'already_blocked') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_blockable', 'reversal_state', CAST(v_cp_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Fence: lease holder on a REVERSING workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Reverse ORDER: only the current top active checkpoint may block the unwind.
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

	SET v_event_seq = v_event_seq + 1;

	UPDATE `tb_mf_workflow_checkpoint`
	SET `reversal_state` = 3,
	    `resolution_event_seq` = v_event_seq,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

	-- blocked_resolution(3), RETAINING reverse direction; lease released so the
	-- workflow is no longer auto-claimable on the forward path (needs resolution).
	UPDATE `tb_mf_workflow`
	SET `state` = 3,
	    `current_disposition` = arg_disposition,
	    `continuation` = JSON_OBJECT('pos', 'blocked', 'seq', arg_seq),
	    `lease_owner` = NULL,
	    `lease_expires_at` = NULL,
	    `current_event_seq` = v_event_seq,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, v_event_seq, arg_event_ts, 'compensation_blocked', arg_executor, NULL,
		JSON_OBJECT('seq', arg_seq, 'disposition', arg_disposition, 'reason', arg_reason, 'direction', 'reverse')
	);

	SELECT JSON_OBJECT('outcome', 'blocked') AS result;
END $$
DELIMITER ;
