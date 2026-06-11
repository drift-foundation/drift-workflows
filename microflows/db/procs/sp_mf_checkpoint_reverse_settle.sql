DELIMITER $$
-- Settle a checkpoint's compensation on a durable reverse-SUCCESS
-- (microflows_design.md §6): mark the checkpoint reversed(2), then either descend
-- to the next-highest active checkpoint (stay reversing(2), lease RETAINED) or, if
-- none remain, reach terminal reversed(5) (lease cleared). The reverse result is
-- recorded in the audit event only as the durable fact of compensation, never
-- overwriting the checkpoint's forward payload.
--
-- The settling reverse op must match the one that was requested
-- (reverse_invocation_id), and the target must be the current TOP active
-- checkpoint (reverse order enforced). Fenced on REVERSING(2); time-disciplined.
-- already_reversed is checked BEFORE the fence (lease-independent): the terminal
-- settle clears the lease, so a lost-ack retry must still resolve to
-- already_reversed rather than fence_lost.
CREATE PROCEDURE `sp_mf_checkpoint_reverse_settle`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_seq int,
	IN arg_reverse_id varbinary(16),
	IN arg_result_json mediumtext,
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
	DECLARE v_next_seq int DEFAULT NULL;
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
	IF arg_result_json IS NULL OR NOT JSON_VALID(arg_result_json) OR JSON_TYPE(arg_result_json) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfResultJsonInvalid';
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

	-- IDENTITY before replay (like operation_settle's op_id check): the settling
	-- reverse op must be the persisted one, else after settlement ANY id would
	-- replay as the same command. revid is NULL only for an active, never-dispatched
	-- checkpoint. Both checks are lease-independent (read-only).
	IF v_cp_revid IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'not_requested') AS result;
		LEAVE proc;
	END IF;
	IF v_cp_revid <> arg_reverse_id THEN
		SELECT JSON_OBJECT('outcome', 'reverse_id_mismatch') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent lost-ack retry (lease-independent — the terminal settle cleared
	-- the lease, so this must resolve even with no live token), AFTER identity.
	IF v_cp_state = 2 THEN
		SELECT JSON_OBJECT('outcome', 'already_reversed') AS result;
		LEAVE proc;
	END IF;
	-- A blocked/resolved checkpoint cannot be settled by the auto loop.
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

	-- Commit the compensation.
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
			arg_workflow_id, v_event_seq, arg_event_ts, 'compensation_settled', arg_executor, NULL,
			JSON_OBJECT('seq', arg_seq, 'terminal', 'reversed')
		);

		SELECT JSON_OBJECT('outcome', 'reversed') AS result;
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
		arg_workflow_id, v_event_seq, arg_event_ts, 'compensation_settled', arg_executor, NULL,
		JSON_OBJECT('seq', arg_seq, 'next_seq', v_next_seq)
	);

	SELECT JSON_OBJECT('outcome', 'reversing', 'next_seq', CAST(v_next_seq AS SIGNED)) AS result;
END $$
DELIMITER ;
