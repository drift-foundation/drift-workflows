DELIMITER $$
-- Create a workflow instance + PIN its manual-IR forward plan (hash + length) + its
-- 'created' event, as ONE atomic command (§24.6 D4). The pin is decided by CREATION:
-- the workflow's creator fixes the plan, not whichever worker claims first. A later
-- submission with the SAME workflow_id but a DIFFERENT plan returns 'plan_conflict'
-- (committed-command resolution never silently adopts a changed plan). plan_length
-- makes operation finality durable (the settle proc derives it). The plan row is
-- written ONLY here, in the same transaction as the workflow row, so it can never be
-- orphaned and the create command is its sole author.
--
-- Idempotent by workflow_id (the PK INSERT serializes). Discipline (§24.4): all time
-- values caller-supplied + stored unchanged; the dup handler is an EXPLICIT 1062
-- handler scoped to the projection INSERT only.
CREATE PROCEDURE `sp_mf_workflow_create_planned`(
	IN arg_workflow_id varbinary(16),
	IN arg_script_name varchar(128),
	IN arg_script_revision int,
	IN arg_event_ts datetime(6),
	IN arg_next_attempt_at datetime(6),
	IN arg_continuation mediumtext,
	IN arg_event_payload mediumtext,
	IN arg_plan_hash varbinary(16),
	IN arg_plan_length int
)
proc:BEGIN
	DECLARE v_exists tinyint(1) DEFAULT 0;
	DECLARE v_pin_missing tinyint(1) DEFAULT 0;
	DECLARE v_hash varbinary(16);
	DECLARE v_length int;
	DECLARE v_script_name varchar(128);
	DECLARE v_script_revision int;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_script_name IS NULL OR arg_script_name = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfScriptNameInvalid';
	END IF;
	IF arg_script_revision IS NULL OR arg_script_revision < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfScriptRevisionInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;
	IF arg_next_attempt_at IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfNextAttemptAtInvalid';
	END IF;
	IF arg_continuation IS NULL OR JSON_VALID(arg_continuation) = 0 OR JSON_TYPE(arg_continuation) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfContinuationInvalid';
	END IF;
	IF arg_event_payload IS NULL OR JSON_VALID(arg_event_payload) = 0 OR JSON_TYPE(arg_event_payload) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventPayloadInvalid';
	END IF;
	IF arg_plan_hash IS NULL OR LENGTH(arg_plan_hash) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfPlanHashInvalid';
	END IF;
	IF arg_plan_length IS NULL OR arg_plan_length < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfPlanLengthInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR 1062 SET v_exists = 1;
		INSERT INTO `tb_mf_workflow` (
			`workflow_id`, `script_name`, `script_revision`, `state`, `execution_direction`,
			`current_disposition`, `current_event_seq`, `current_event_ts`, `fencing_token`,
			`lease_owner`, `lease_expires_at`, `next_attempt_at`, `current_operation_attempt`,
			`continuation`, `created_at`, `updated_at`
		) VALUES (
			arg_workflow_id, arg_script_name, arg_script_revision, 1, 1,
			0, 1, arg_event_ts, 0,
			NULL, NULL, arg_next_attempt_at, 0,
			arg_continuation, arg_event_ts, arg_event_ts
		);
	END;

	IF v_exists = 1 THEN
		-- Already created: committed-command resolution. Accept 'exists' iff the FULL
		-- immutable identity is unchanged — the pinned plan (hash + length) AND the
		-- pinned script (name + revision). A missing pin (created as a non-plan
		-- workflow) or ANY mismatch is plan_conflict.
		SELECT `script_name`, `script_revision` INTO v_script_name, v_script_revision
		FROM `tb_mf_workflow` WHERE `workflow_id` = arg_workflow_id;
		BEGIN
			DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_pin_missing = 1;
			SELECT `plan_hash`, `plan_length` INTO v_hash, v_length
			FROM `tb_mf_workflow_plan` WHERE `workflow_id` = arg_workflow_id;
		END;
		IF v_pin_missing = 1 OR NOT (v_hash <=> arg_plan_hash) OR v_length <> arg_plan_length
		   OR NOT (v_script_name <=> arg_script_name) OR v_script_revision <> arg_script_revision THEN
			SELECT JSON_OBJECT('outcome', 'plan_conflict',
				'plan_length', CAST(COALESCE(v_length, 0) AS SIGNED)) AS result;
			LEAVE proc;
		END IF;
		SELECT JSON_OBJECT('outcome', 'exists') AS result;
		LEAVE proc;
	END IF;

	-- Fresh: pin the plan + append the 'created' event in this same transaction.
	INSERT INTO `tb_mf_workflow_plan` (`workflow_id`, `plan_hash`, `plan_length`, `created_at`)
	VALUES (arg_workflow_id, arg_plan_hash, arg_plan_length, arg_event_ts);

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, 1, arg_event_ts, 'created', NULL, NULL, arg_event_payload
	);

	SELECT JSON_OBJECT('outcome', 'created') AS result;
END $$
DELIMITER ;
