DELIMITER $$
-- Read a workflow's durable pinned plan identity (§10/§22): the immutable
-- `(script_name, plan_version, content_hash, plan_length)` it was created under.
--
-- The runner reads THIS FIRST (before any registry access): an existing workflow's
-- MODE (planned vs legacy) and its pinned plan come from STORAGE, not from current
-- config — so terminal replay and a pinned-version resume never depend on registry
-- availability and a changed/absent config plan cannot misroute an existing planned
-- workflow. Returns 'not_found' for a workflow with no plan pin (absent, or a legacy
-- non-plan workflow); the runner then takes the create-if-absent / legacy path.
--
-- Read-only: no lease/fence, like operation_request_get.
CREATE PROCEDURE `sp_mf_plan_get`(
	IN arg_workflow_id varbinary(16)
)
proc:BEGIN
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_script_name varchar(128);
	DECLARE v_plan_version varchar(32);
	DECLARE v_content_hash varbinary(33);
	DECLARE v_plan_length int;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `w`.`script_name`, `p`.`plan_version`, `p`.`content_hash`, `p`.`plan_length`
		INTO v_script_name, v_plan_version, v_content_hash, v_plan_length
		FROM `tb_mf_workflow_plan` `p`
		JOIN `tb_mf_workflow` `w` ON `w`.`workflow_id` = `p`.`workflow_id`
		WHERE `p`.`workflow_id` = arg_workflow_id;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	SELECT JSON_OBJECT('outcome', 'found',
		'script_name', v_script_name,
		'plan_version', v_plan_version,
		'content_hash', LOWER(HEX(v_content_hash)),
		'plan_length', CAST(v_plan_length AS SIGNED)) AS result;
END $$
DELIMITER ;
