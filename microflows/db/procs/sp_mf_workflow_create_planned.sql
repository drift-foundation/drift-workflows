DELIMITER $$
-- Create a workflow instance + PIN its manual-IR forward plan (version + hash +
-- length) + its 'created' event, as ONE atomic command (§24.6 D4). The pin is decided
-- by CREATION: the workflow's creator fixes the plan, not whichever worker claims
-- first. The pin is `(script_name, plan_version, content_hash, plan_length)`, where
-- plan_version is a validated semantic version (major.minor.patch). plan_length makes
-- operation finality durable (the settle proc derives it). The plan row is written
-- ONLY here, in the same transaction as the workflow row, so it can never be orphaned
-- and the create command is its sole author.
--
-- CREATION-RACE resolution (committed-command, §24.6), scoped to the SAME plan NAME: on
-- 'exists' for the same script_name, RETURN THE WINNING DURABLE PIN — the first create
-- fixed it, every later create just observes it. We do NOT conflict when only the version
-- / hash / length differ: the caller adopts the winner and exact-match-resolves it against
-- its own loaded generation (a mismatch becomes a recoverable revision_unavailable at the
-- runner, never a substitution here). 'plan_conflict' is returned for a CONTRADICTORY
-- identity on the workflow_id: (a) it exists under a DIFFERENT plan name (an id collision
-- across plans — must not silently adopt the other plan), or (b) it exists as a NON-plan
-- (legacy) workflow with no pin to return and cannot be reinterpreted as planned.
--
-- Idempotent by workflow_id (the PK INSERT serializes). Discipline (§24.4): all time
-- values caller-supplied + stored unchanged; the dup handler is an EXPLICIT 1062
-- handler scoped to the projection INSERT only. (tb_mf_workflow.script_revision is a
-- legacy int set to 1 for planned workflows; the plan model keys on plan_version.)
CREATE PROCEDURE `sp_mf_workflow_create_planned`(
	IN arg_workflow_id varbinary(16),
	IN arg_script_name varchar(128),
	IN arg_plan_version varchar(32),
	IN arg_event_ts datetime(6),
	IN arg_next_attempt_at datetime(6),
	IN arg_continuation mediumtext,
	IN arg_event_payload mediumtext,
	IN arg_content_hash varbinary(33),
	IN arg_plan_length int
)
proc:BEGIN
	DECLARE v_exists tinyint(1) DEFAULT 0;
	DECLARE v_pin_missing tinyint(1) DEFAULT 0;
	DECLARE v_hash varbinary(33);
	DECLARE v_length int;
	DECLARE v_version varchar(32);
	DECLARE v_script_name varchar(128);

	-- Atomicity is CALLER-OWNED (uniform with every other SP): the workflow + plan + event
	-- inserts run in the caller's transaction and publish together at the caller's COMMIT
	-- (the host uses autocommit=false + rpc.commit per call). The procedure NEVER manages
	-- its own transaction — issuing START TRANSACTION / COMMIT here would implicitly commit
	-- and seize publication from the caller. Because the workflow PK INSERT holds its lock
	-- until that COMMIT, a racing creator blocks until the winner's FULL pin is durable and
	-- then reads the complete pin (never a partial / spurious-conflict read).
	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_script_name IS NULL OR arg_script_name = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfScriptNameInvalid';
	END IF;
	-- Immutable semantic version: validated shape (major.minor.patch).
	IF arg_plan_version IS NULL OR arg_plan_version NOT REGEXP '^[0-9]+\\.[0-9]+\\.[0-9]+$' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfPlanVersionInvalid';
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
	IF arg_content_hash IS NULL OR LENGTH(arg_content_hash) <> 33 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfContentHashInvalid';
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
			arg_workflow_id, arg_script_name, 1, 1, 1,
			0, 1, arg_event_ts, 0,
			NULL, NULL, arg_next_attempt_at, 0,
			arg_continuation, arg_event_ts, arg_event_ts
		);
	END;

	IF v_exists = 1 THEN
		-- Already created: CREATION-RACE resolution, scoped to the SAME plan NAME. The
		-- workflow_id's CONTRACT is the plan name; competing creates of the SAME name (e.g.
		-- different versions of checkout) adopt the WINNING durable pin
		-- `(script_name, plan_version, content_hash, plan_length)` and exact-match-resolve
		-- locally. But a create for a DIFFERENT plan name (an id collision — e.g. checkout-v2
		-- meeting an existing billing-v1 workflow) must NOT silently adopt the other plan:
		-- that is a contradictory identity -> 'plan_conflict'. A workflow_id that exists with
		-- NO plan pin (a legacy non-plan workflow) likewise conflicts.
		-- Our INSERT blocked on the winner's PK lock until its COMMIT, so the winner's full
		-- pin (workflow + plan) is now durably visible.
		SELECT `script_name` INTO v_script_name
		FROM `tb_mf_workflow` WHERE `workflow_id` = arg_workflow_id;
		BEGIN
			DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_pin_missing = 1;
			SELECT `plan_version`, `content_hash`, `plan_length` INTO v_version, v_hash, v_length
			FROM `tb_mf_workflow_plan` WHERE `workflow_id` = arg_workflow_id;
		END;
		IF v_pin_missing = 1 OR NOT (v_script_name <=> arg_script_name) THEN
			SELECT JSON_OBJECT('outcome', 'plan_conflict',
				'plan_length', CAST(COALESCE(v_length, 0) AS SIGNED)) AS result;
			LEAVE proc;
		END IF;
		SELECT JSON_OBJECT('outcome', 'exists',
			'script_name', v_script_name, 'plan_version', v_version,
			'content_hash', LOWER(HEX(v_hash)), 'plan_length', CAST(v_length AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Fresh: pin the plan + append the 'created' event in this same transaction.
	INSERT INTO `tb_mf_workflow_plan` (`workflow_id`, `plan_version`, `content_hash`, `plan_length`, `created_at`)
	VALUES (arg_workflow_id, arg_plan_version, arg_content_hash, arg_plan_length, arg_event_ts);

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, 1, arg_event_ts, 'created', NULL, NULL, arg_event_payload
	);

	-- Return the freshly-pinned identity (= the supplied active generation). The caller
	-- commits the workflow + plan + event together.
	SELECT JSON_OBJECT('outcome', 'created',
		'script_name', arg_script_name, 'plan_version', arg_plan_version,
		'content_hash', LOWER(HEX(arg_content_hash)), 'plan_length', CAST(arg_plan_length AS SIGNED)) AS result;
END $$
DELIMITER ;
