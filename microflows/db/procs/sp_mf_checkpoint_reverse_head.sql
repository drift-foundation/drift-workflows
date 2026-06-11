DELIMITER $$
-- Authoritative reverse cursor (§6): the TOP (highest-seq) ACTIVE checkpoint of a
-- workflow. The CHECKPOINT STACK is the authority for what to compensate next; the
-- workflow continuation is only a projection, so a resuming worker reads THIS, not
-- the continuation, to decide the next reverse action.
--
-- Read-only (no lease/fence), like operation_request_get. Outcomes:
--   none_active  — no active checkpoints (the unwind is complete)
--   pending      — top active checkpoint not yet dispatched: returns the forward
--                  identity (operation name + result payload) so the caller can
--                  derive + persist the reverse binding (reverse_request) first
--   dispatched   — a compensation was already dispatched: returns the DURABLE
--                  pinned binding (reverse contract + input identity + invocation
--                  id) so the caller reconciles/redispatches against THAT contract,
--                  immune to later registry/manual-IR changes
CREATE PROCEDURE `sp_mf_checkpoint_reverse_head`(
	IN arg_workflow_id varbinary(16)
)
proc:BEGIN
	DECLARE v_seq int;
	DECLARE v_op_name varchar(128);
	DECLARE v_payload mediumtext;
	DECLARE v_revid varbinary(16);
	DECLARE v_rname varchar(128);
	DECLARE v_rsv int;
	DECLARE v_rinput mediumtext;
	DECLARE v_rhash varchar(64);
	DECLARE v_missing tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `seq`, `operation_name`, `payload`, `reverse_invocation_id`,
		       `reverse_operation_name`, `reverse_schema_version`, `reverse_input_json`, `reverse_input_hash`
		INTO v_seq, v_op_name, v_payload, v_revid, v_rname, v_rsv, v_rinput, v_rhash
		FROM `tb_mf_workflow_checkpoint`
		WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1
		ORDER BY `seq` DESC
		LIMIT 1;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'none_active') AS result;
		LEAVE proc;
	END IF;

	IF v_revid IS NULL THEN
		SELECT JSON_OBJECT('outcome', 'pending',
			'seq', CAST(v_seq AS SIGNED),
			'operation_name', v_op_name,
			'payload', JSON_EXTRACT(v_payload, '$')) AS result;
		LEAVE proc;
	END IF;

	SELECT JSON_OBJECT('outcome', 'dispatched',
		'seq', CAST(v_seq AS SIGNED),
		'operation_name', v_op_name,
		'reverse_invocation_id', LOWER(HEX(v_revid)),
		'reverse_operation_name', v_rname,
		'reverse_schema_version', CAST(v_rsv AS SIGNED),
		'reverse_input_json', JSON_EXTRACT(v_rinput, '$'),
		'reverse_input_hash', v_rhash) AS result;
END $$
DELIMITER ;
