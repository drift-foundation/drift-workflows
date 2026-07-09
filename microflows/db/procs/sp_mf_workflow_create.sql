DELIMITER $$
-- Create a workflow instance + its 'created' event as one
-- atomic publication (§24.6 D4: every transition commits with its event).
--
-- Idempotent by stable command ID = workflow_id: the PK INSERT is the
-- serializer; a duplicate returns {'outcome':'exists'} without reading or
-- mutating anything — committed-command resolution before any new event is
-- derived or appended (§24.4).
--
-- Discipline (§24.4): all time values are caller-supplied and stored
-- unchanged; no clock reads; no auto-generated identifiers. The duplicate
-- handler is an EXPLICIT handler for ER_DUP_ENTRY (1062) scoped to the
-- projection INSERT only — never INSERT IGNORE (which would downgrade
-- truncation/type errors to warnings misread as "exists").
CREATE PROCEDURE `sp_mf_workflow_create`(
	IN arg_workflow_id varbinary(16),
	IN arg_script_name varchar(128),
	IN arg_script_revision int,
	IN arg_event_ts datetime(6),
	IN arg_next_attempt_at datetime(6),
	IN arg_continuation mediumtext,
	IN arg_event_payload mediumtext
)
proc:BEGIN
	DECLARE v_exists tinyint(1) DEFAULT 0;

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

	BEGIN
		DECLARE CONTINUE HANDLER FOR 1062 SET v_exists = 1;
		INSERT INTO `tb_mf_workflow` (
			`workflow_id`,
			`script_name`,
			`script_revision`,
			`state`,
			`execution_direction`,
			`current_disposition`,
			`current_event_ts`,
			`fencing_token`,
			`lease_owner`,
			`lease_expires_at`,
			`next_attempt_at`,
			`current_operation_attempt`,
			`continuation`,
			`created_at`,
			`updated_at`
		) VALUES (
			arg_workflow_id,
			arg_script_name,
			arg_script_revision,
			1,                       -- forward (state)
			1,                       -- forward (execution_direction)
			0,                       -- no disposition
			arg_event_ts,
			0,
			NULL,
			NULL,
			arg_next_attempt_at,
			0,
			arg_continuation,
			arg_event_ts,
			arg_event_ts
		);
	END;

	IF v_exists = 1 THEN
		-- Stable command (workflow_id) already committed: resolve, append nothing.
		SELECT JSON_OBJECT('outcome', 'exists') AS result;
		LEAVE proc;
	END IF;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, arg_event_ts, 'created', NULL, NULL, arg_event_payload
	);

	SELECT JSON_OBJECT('outcome', 'created') AS result;
END $$
DELIMITER ;
