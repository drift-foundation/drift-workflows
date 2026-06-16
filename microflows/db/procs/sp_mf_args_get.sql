DELIMITER $$
-- Read a workflow's durable instance ARGUMENTS (manual-IR frontend, Step 1): the one
-- canonical JSON object it was created under.
--
-- Resume reads THIS — the durable, immutable record — never submission/CLI input, so a
-- restarted worker drives branches/transforms from the authoritative instance data. Returns
-- 'not_found' for a workflow with no args row (absent, or a pre-args fixture). The canonical
-- bytes are re-emitted as a JSON object value (`args`).
--
-- Read-only: no lease/fence, like operation_request_get / plan_get.
CREATE PROCEDURE `sp_mf_args_get`(
	IN arg_workflow_id varbinary(16)
)
proc:BEGIN
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_args mediumblob;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `args_canonical` INTO v_args
		FROM `tb_mf_workflow_args` WHERE `workflow_id` = arg_workflow_id;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	-- The stored bytes ARE canonical JSON; re-emit them as a nested object value.
	SELECT JSON_OBJECT('outcome', 'found',
		'args', JSON_EXTRACT(CONVERT(v_args USING utf8mb4), '$')) AS result;
END $$
DELIMITER ;
