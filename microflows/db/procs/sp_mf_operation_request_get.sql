DELIMITER $$
-- Load the durable operation REQUEST (microflows_design.md §2.5). A restarted
-- standalone worker that resumes a workflow must use the persisted request
-- (operation id, name, input) as authoritative — NOT re-derive it from CLI
-- arguments — so omitted or different CLI input cannot cause a spurious
-- operation_conflict. Recovery reads this; a fresh workflow has no row yet.
--
-- Outcomes:
--   found { operation_id, operation_name, input_json, input_hash, status }
--   not_found
CREATE PROCEDURE `sp_mf_operation_request_get`(
	IN arg_workflow_id varbinary(16),
	IN arg_operation_seq int
)
proc:BEGIN
	DECLARE v_op_id varbinary(16);
	DECLARE v_op_name varchar(128);
	DECLARE v_input mediumtext;
	DECLARE v_hash varchar(64);
	DECLARE v_status tinyint;
	DECLARE v_missing tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_operation_seq IS NULL OR arg_operation_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationSeqInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `operation_id`, `operation_name`, `input_json`, `input_hash`, `status`
		INTO v_op_id, v_op_name, v_input, v_hash, v_status
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	SELECT JSON_OBJECT(
		'outcome', 'found',
		'operation_id', LOWER(HEX(v_op_id)),
		'operation_name', v_op_name,
		'input_json', JSON_EXTRACT(v_input, '$'),
		'input_hash', v_hash,
		'status', CAST(v_status AS SIGNED)
	) AS result;
END $$
DELIMITER ;
