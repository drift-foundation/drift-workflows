DELIMITER $$
-- Read-only workflow inspection: distinguish terminal vs active(leased) vs
-- claimable/deferred vs absent. Used by a driver when a targeted claim returns
-- not_claimable, to decide whether to replay a terminal result, back off, or
-- report contention. No mutation, no lease.
--
-- Outcomes:
--   found { state, execution_direction, current_disposition, is_terminal,
--           leased (1 if an unexpired lease is held at db_now), continuation,
--           terminal_reason, workflow_return_json }
--   not_found
CREATE PROCEDURE `sp_mf_workflow_inspect`(
	IN arg_workflow_id varbinary(16),
	IN arg_db_now datetime(6)
)
proc:BEGIN
	DECLARE v_state tinyint;
	DECLARE v_dir tinyint;
	DECLARE v_disp tinyint;
	DECLARE v_owner varbinary(16);
	DECLARE v_expires datetime(6);
	DECLARE v_cont mediumtext;
	DECLARE v_term_reason varchar(190) DEFAULT NULL;
	DECLARE v_return mediumtext DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_terminal tinyint(1) DEFAULT 0;
	DECLARE v_leased tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_db_now IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfDbNowInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `state`, `execution_direction`, `current_disposition`,
		       `lease_owner`, `lease_expires_at`, `continuation`, `terminal_reason`,
		       `workflow_return_json`
		INTO v_state, v_dir, v_disp, v_owner, v_expires, v_cont, v_term_reason,
		     v_return
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_state IN (4,5,6,7) THEN SET v_terminal = 1; END IF;
	IF v_owner IS NOT NULL AND v_expires >= arg_db_now THEN SET v_leased = 1; END IF;

	-- CAST to SIGNED so JSON_OBJECT emits JSON NUMBERS, not strings (local
	-- variables otherwise serialize as quoted strings, unlike column refs).
	SELECT JSON_OBJECT(
		'outcome', 'found',
		'state', CAST(v_state AS SIGNED),
		'execution_direction', CAST(v_dir AS SIGNED),
		'current_disposition', CAST(v_disp AS SIGNED),
		'is_terminal', CAST(v_terminal AS SIGNED),
		'leased', CAST(v_leased AS SIGNED),
		'continuation', JSON_EXTRACT(v_cont, '$'),
		-- Durable terminal reason (NULL on non-failure terminals); replay renders from THIS, never recomputed.
		'terminal_reason', v_term_reason,
		-- Durable workflow TERMINAL RETURN (1b.0a step 3) — the authoritative typed workflow
		-- return; NULL until completed (state=4), where ck_mf_workflow_state_return guarantees
		-- a valid JSON-object value. Terminal replay reads THIS, never re-derives from the graph.
		'workflow_return_json', JSON_EXTRACT(v_return, '$')
	) AS result;
END $$
DELIMITER ;
