DELIMITER $$
-- Claim ONE specific workflow by id (§24.2), for a single-workflow driver that
-- must not accidentally claim a different instance of the same script. Same
-- fencing/lease semantics as sp_mf_workflow_claim, but targeted: it claims only
-- if THIS workflow is claimable (forward/reversing, due, unleased/expired).
--
-- Outcomes: claimed (the resume snapshot) | not_claimable (exists but leased,
-- not due, or terminal) | not_found. db_now/lease are caller-sourced (§24.4).
CREATE PROCEDURE `sp_mf_workflow_claim_by_id`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_db_now datetime(6),
	IN arg_lease_expires_at datetime(6)
)
proc:BEGIN
	DECLARE v_id varbinary(16);
	DECLARE v_none tinyint(1) DEFAULT 0;
	DECLARE v_exists tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_executor IS NULL OR LENGTH(arg_executor) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfExecutorInvalid';
	END IF;
	IF arg_db_now IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfDbNowInvalid';
	END IF;
	IF arg_lease_expires_at IS NULL OR arg_lease_expires_at <= arg_db_now THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfLeaseExpiryInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_none = 1;
		SELECT `workflow_id` INTO v_id
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		  AND `state` IN (1,2)
		  AND `next_attempt_at` <= arg_db_now
		  AND (`lease_owner` IS NULL OR `lease_expires_at` < arg_db_now)
		FOR UPDATE SKIP LOCKED;
	END;

	IF v_none = 1 THEN
		SELECT COUNT(*) INTO v_exists FROM `tb_mf_workflow` WHERE `workflow_id` = arg_workflow_id;
		IF v_exists = 0 THEN
			SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		ELSE
			SELECT JSON_OBJECT('outcome', 'not_claimable') AS result;
		END IF;
		LEAVE proc;
	END IF;

	UPDATE `tb_mf_workflow`
	SET `lease_owner` = arg_executor,
	    `lease_expires_at` = arg_lease_expires_at,
	    `fencing_token` = `fencing_token` + 1,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = v_id;

	SELECT JSON_OBJECT(
		'outcome', 'claimed',
		'workflow_id', LOWER(HEX(`workflow_id`)),
		'fencing_token', `fencing_token`,
		'state', `state`,
		'execution_direction', `execution_direction`,
		'current_disposition', `current_disposition`,
		'script_name', `script_name`,
		'script_revision', `script_revision`,
		'current_operation_attempt', `current_operation_attempt`,
		'continuation', JSON_EXTRACT(`continuation`, '$')
	) AS result
	FROM `tb_mf_workflow`
	WHERE `workflow_id` = v_id;
END $$
DELIMITER ;
