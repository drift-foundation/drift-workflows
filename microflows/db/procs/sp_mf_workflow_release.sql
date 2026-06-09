DELIMITER $$
-- Fenced voluntary lease release (§24.5): the holder clears its lease and
-- schedules the next attempt (immediately, or with backoff — durable backoff
-- lives on the row so a backed-off workflow is not pinned to its worker).
-- Lease axis only: no lifecycle transition, no event append.
--
-- arg_next_attempt_at is the caller-supplied database-sourced due time
-- (§24.4); nothing here reads a clock. Same lock-compare-update fencing
-- pattern as sp_mf_workflow_heartbeat.
CREATE PROCEDURE `sp_mf_workflow_release`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_db_now datetime(6),
	IN arg_next_attempt_at datetime(6)
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
	IF arg_next_attempt_at IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfNextAttemptAtInvalid';
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

	-- A release is legal while the fence holds, even if the lease just
	-- expired (releasing an expired-but-unreclaimed lease is harmless and
	-- lets a shutting-down worker be tidy): owner + token must match and the
	-- state must still be claimable; expiry is NOT checked here, unlike
	-- heartbeat — once another executor claims, the token mismatch fences us.
	IF v_owner IS NULL OR v_owner <> arg_executor
	   OR v_token <> arg_fencing_token
	   OR v_state NOT IN (1,2) THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	UPDATE `tb_mf_workflow`
	SET `lease_owner` = NULL,
	    `lease_expires_at` = NULL,
	    `next_attempt_at` = arg_next_attempt_at,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id;

	SELECT JSON_OBJECT('outcome', 'released') AS result;
END $$
DELIMITER ;
