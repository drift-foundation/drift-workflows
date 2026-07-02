DELIMITER $$
-- Composition (1b.1): read-only inspection of a workflow call, for the parent's
-- await/reconcile loop AND operator inspect. PURE read — no fence check, no write, no
-- commit-relevant side effect, mirroring sp_mf_workflow_inspect exactly. child_status
-- returned here is the sidecar's OWN (possibly-stale) hint, read but never written by
-- this procedure — refreshing it is sp_mf_child_terminal_notify's (terminal) or
-- sp_mf_call_hint_refresh's (non-terminal) job, never this one's.
--
-- The child workflow row is AUTHORITATIVE: state/execution_direction/current_disposition/
-- is_terminal/terminal_reason/workflow_return_json below are read directly from it, never
-- from the sidecar hint. The runner settles/fails the parent's call operation from THESE
-- fields, never from child_status.
--
-- Outcomes:
--   found     { child_workflow_id, child_status (hint), state, execution_direction,
--               current_disposition, is_terminal, terminal_reason, workflow_return_json }
--   not_found (no tb_mf_call row for this (workflow_id, operation_seq) — not a call operation,
--              or the operation doesn't exist at all)
CREATE PROCEDURE `sp_mf_call_inspect`(
	IN arg_workflow_id varbinary(16),
	IN arg_operation_seq int
)
proc:BEGIN
	DECLARE v_child_id varbinary(16);
	DECLARE v_child_status tinyint;
	DECLARE v_state tinyint;
	DECLARE v_dir tinyint;
	DECLARE v_disp tinyint;
	DECLARE v_term_reason varchar(190) DEFAULT NULL;
	DECLARE v_return mediumtext DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_child_missing tinyint(1) DEFAULT 0;
	DECLARE v_terminal tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_operation_seq IS NULL OR arg_operation_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationSeqInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `child_workflow_id`, `child_status`
		INTO v_child_id, v_child_status
		FROM `tb_mf_call`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- The child row is created in the SAME transaction as this sidecar row (sp_mf_call_submit)
	-- and the FK fk_mf_call_child guarantees it still exists, so this lookup cannot legitimately
	-- miss; treat a miss as not_found (defensive, matches the read-only/no-lock shape here).
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_child_missing = 1;
		SELECT `state`, `execution_direction`, `current_disposition`, `terminal_reason`, `workflow_return_json`
		INTO v_state, v_dir, v_disp, v_term_reason, v_return
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = v_child_id;
	END;

	IF v_child_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_state IN (4,5,6,7) THEN SET v_terminal = 1; END IF;

	SELECT JSON_OBJECT(
		'outcome', 'found',
		'child_workflow_id', LOWER(HEX(v_child_id)),
		'child_status', CAST(v_child_status AS SIGNED),
		'state', CAST(v_state AS SIGNED),
		'execution_direction', CAST(v_dir AS SIGNED),
		'current_disposition', CAST(v_disp AS SIGNED),
		'is_terminal', CAST(v_terminal AS SIGNED),
		'terminal_reason', v_term_reason,
		'workflow_return_json', JSON_EXTRACT(v_return, '$')
	) AS result;
END $$
DELIMITER ;
