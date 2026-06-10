DELIMITER $$
-- Read the LOCAL authoritative result of an operation (microflows_design.md
-- §2.5). Microflows persists the result on settle, so a completed workflow
-- stays inspectable even when the participant is unavailable — recovery and
-- terminal replay never depend on the remote.
--
-- Outcomes:
--   succeeded { result } : a durable success result is recorded locally
--   requested            : the operation exists but has no result yet
--   not_found            : no such operation
CREATE PROCEDURE `sp_mf_operation_result`(
	IN arg_workflow_id varbinary(16),
	IN arg_operation_seq int
)
proc:BEGIN
	DECLARE v_status tinyint;
	DECLARE v_result mediumtext;
	DECLARE v_missing tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_operation_seq IS NULL OR arg_operation_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationSeqInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `status`, `result_json`
		INTO v_status, v_result
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_status = 2 THEN
		SELECT JSON_OBJECT('outcome', 'succeeded', 'result', JSON_EXTRACT(v_result, '$')) AS result;
	ELSE
		SELECT JSON_OBJECT('outcome', 'requested') AS result;
	END IF;
END $$
DELIMITER ;
