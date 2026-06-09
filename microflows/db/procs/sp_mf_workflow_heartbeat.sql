DELIMITER $$
-- Fenced lease extension (§24.3, §24.5). Valid only while the caller still
-- holds an unexpired lease with the exact fencing token on a claimable
-- lifecycle state. Lease axis only: no lifecycle transition, no event.
--
-- Pattern: lock the row, compare the fence deterministically, then update —
-- never ROW_COUNT() on a fenced UPDATE (a value-identical retry would report
-- 0 changed rows and masquerade as a lost fence).
--
-- arg_db_now and arg_lease_expires_at are caller-supplied database-sourced
-- values (§24.4); nothing here reads a clock.
CREATE PROCEDURE `sp_mf_workflow_heartbeat`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_db_now datetime(6),
	IN arg_lease_expires_at datetime(6)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_expires datetime(6);
	DECLARE v_state tinyint;
	DECLARE v_missing tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_executor IS NULL OR LENGTH(arg_executor) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfExecutorInvalid';
	END IF;
	IF arg_fencing_token IS NULL OR arg_fencing_token < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfFencingTokenInvalid';
	END IF;
	IF arg_db_now IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfDbNowInvalid';
	END IF;
	IF arg_lease_expires_at IS NULL OR arg_lease_expires_at <= arg_db_now THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfLeaseExpiryInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `lease_expires_at`, `state`
		INTO v_owner, v_token, v_expires, v_state
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_owner IS NULL OR v_owner <> arg_executor
	   OR v_token <> arg_fencing_token
	   OR v_expires < arg_db_now
	   OR v_state NOT IN (1,2) THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	UPDATE `tb_mf_workflow`
	SET `lease_expires_at` = arg_lease_expires_at,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id;

	SELECT JSON_OBJECT('outcome', 'extended') AS result;
END $$
DELIMITER ;
