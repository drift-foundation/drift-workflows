DELIMITER $$
-- Composition (1b.1): refresh the sidecar's DISPLAY-ONLY child_status hint (§Liveness) after a
-- non-terminal call_inspect poll (in particular `blocked`, since sp_mf_child_terminal_notify only
-- ever fires on a child TERMINAL — this is the only path that keeps the hint current for a
-- merely-blocked child). Explicitly best-effort: NO fence check (this is a display hint, never a
-- value of record — the child workflow row stays the sole authority the runner settles/fails
-- from), so a lost/duplicate/delayed call here has zero correctness impact, only a stale hint an
-- operator sees for slightly longer.
--
-- Monotonic by TIME (not by child_status value): a refresh whose arg_event_ts is not strictly
-- after the sidecar's own last_inspected_at is a no-op (stale/out-of-order poll result arriving
-- late must never clobber a fresher one), never an error — this procedure never SIGNALs or
-- returns not_found for a mere ordering loss, only for a genuinely missing row.
--
-- Outcomes:
--   refreshed  — hint updated (or arg_event_ts was not after the stored last_inspected_at, a
--                harmless no-op — same outcome either way; best-effort has no failure mode here)
--   not_found  — no tb_mf_call row for this (workflow_id, operation_seq)
CREATE PROCEDURE `sp_mf_call_hint_refresh`(
	IN arg_workflow_id varbinary(16),
	IN arg_operation_seq int,
	IN arg_child_status tinyint,
	IN arg_event_ts datetime(6)
)
proc:BEGIN
	DECLARE v_last_inspected datetime(6) DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_operation_seq IS NULL OR arg_operation_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationSeqInvalid';
	END IF;
	IF arg_child_status IS NULL OR arg_child_status NOT IN (1,2,3,4) THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildStatusInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `last_inspected_at` INTO v_last_inspected
		FROM `tb_mf_call`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_last_inspected IS NULL OR arg_event_ts > v_last_inspected THEN
		UPDATE `tb_mf_call`
		SET `child_status` = arg_child_status,
		    `last_inspected_at` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END IF;

	SELECT JSON_OBJECT('outcome', 'refreshed') AS result;
END $$
DELIMITER ;
