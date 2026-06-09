DELIMITER $$
-- Claim the next due workflow instance for one script (§24.2).
--
-- Two statements in the caller's transaction: SELECT ... FOR UPDATE SKIP
-- LOCKED picks the next due instance without blocking on rows other
-- executors are claiming concurrently; the UPDATE installs the
-- caller-computed lease and bumps the fencing token.
--
-- The claimable predicate (§24.1) compares against the caller-supplied
-- database-sourced now (arg_db_now); nothing here reads a clock (§24.4).
--
-- Scoped by script_name: in the manual-IR milestone an executor may only
-- run scripts in its in-process registry, so it claims per registered
-- script. (Also gives test runs natural isolation via unique script names.)
--
-- Claims touch only the lease axis: no lifecycle transition, no event
-- append — "running" is a claimable state holding a valid lease (§24.1).
-- Recovery is NOT special: an expired lease on a claimable state simply
-- matches the predicate; the fencing bump dooms any stale holder's
-- publications.
CREATE PROCEDURE `sp_mf_workflow_claim`(
	IN arg_script_name varchar(128),
	IN arg_executor varbinary(16),
	IN arg_db_now datetime(6),
	IN arg_lease_expires_at datetime(6)
)
proc:BEGIN
	DECLARE v_id varbinary(16);
	DECLARE v_none tinyint(1) DEFAULT 0;

	IF arg_script_name IS NULL OR arg_script_name = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfScriptNameInvalid';
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
		WHERE `script_name` = arg_script_name
		  AND `state` IN (1,2)
		  AND `next_attempt_at` <= arg_db_now
		  AND (`lease_owner` IS NULL OR `lease_expires_at` < arg_db_now)
		ORDER BY `next_attempt_at`, `workflow_id`
		LIMIT 1
		FOR UPDATE SKIP LOCKED;
	END;

	IF v_none = 1 THEN
		SELECT JSON_OBJECT('outcome', 'none') AS result;
		LEAVE proc;
	END IF;

	UPDATE `tb_mf_workflow`
	SET `lease_owner` = arg_executor,
	    `lease_expires_at` = arg_lease_expires_at,
	    `fencing_token` = `fencing_token` + 1,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = v_id;

	-- The claimed snapshot: everything the executor needs to resume from the
	-- durable continuation under the new fencing token. `continuation` is a
	-- nested JSON object (document transport contract), not JSON-in-a-string.
	SELECT JSON_OBJECT(
		'outcome', 'claimed',
		'workflow_id', LOWER(HEX(`workflow_id`)),
		'fencing_token', `fencing_token`,
		'state', `state`,
		'execution_direction', `execution_direction`,
		'current_disposition', `current_disposition`,
		'script_name', `script_name`,
		'script_revision', `script_revision`,
		'current_event_seq', `current_event_seq`,
		'current_operation_attempt', `current_operation_attempt`,
		'continuation', JSON_EXTRACT(`continuation`, '$')
	) AS result
	FROM `tb_mf_workflow`
	WHERE `workflow_id` = v_id;
END $$
DELIMITER ;
