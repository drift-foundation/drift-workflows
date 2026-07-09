DELIMITER $$
-- Composition (1b.1): sibling of sp_mf_operation_request — persists a workflow call's PARENT-side
-- op row (call_kind=2) + advances the parent's continuation, but ADDITIONALLY, in the SAME
-- transaction, creates the full planned-workflow bundle for the CHILD (tb_mf_workflow +
-- tb_mf_workflow_plan + tb_mf_workflow_args + its own 'created' event, exactly like
-- sp_mf_workflow_create_planned's own shape) + the tb_mf_call sidecar row, and runs the
-- read-only recursion guard. Does NOT call sp_mf_operation_request or
-- sp_mf_workflow_create_planned — this procedure IS their composition.
--
-- STRICT VALIDATE-THEN-MUTATE PHASING (no exceptions) — the host commits UNCONDITIONALLY after
-- reading this procedure's result document, regardless of what the outcome string says (there is
-- no rollback path at the host layer for a "structured rejection"). So every check that can
-- possibly reject — arg-shape, fence, plan-order, existing-row/idempotency, recursion/depth —
-- MUST complete using ONLY reads, before the FIRST write statement of any kind. Once the write
-- phase begins (comment "ONLY NOW" below), nothing can reject any more.
--
-- operation_id = child_workflow_id (§Durable state) is an ENFORCED invariant: the caller supplies
-- both explicitly (mirroring how operation_id is always caller-supplied, never derived,
-- throughout this codebase), and entry validation SIGNALs if they disagree.
--
-- arg_input_json is trusted as ALREADY CANONICAL (ordered-key compact JSON) — this procedure does
-- NOT canonicalize it. It is stored VERBATIM, byte-for-byte, as the child's
-- tb_mf_workflow_args.args_canonical (mirrors sp_mf_workflow_create_planned's own arg_args
-- contract exactly: that procedure's header states "the runner canonicalizes to ordered-key
-- compact form before the call" — canonicalization is the CALLER's responsibility there, and the
-- SAME division of responsibility applies here). The future host wrapper (call_submit) MUST
-- canonicalize the call's input before invoking this procedure, exactly as host.drift's
-- create_planned already does for arg_args. Two byte-different-but-semantically-equal JSON
-- documents are NOT the same request here (idempotent replay compares args_canonical
-- byte-for-byte, like create_planned's own workflow_conflict check) — see
-- db-tests/sp_call_test.py's submit_noncanonical_json_not_idempotent for a regression pin.
--
-- Recursion guard: reconstructs the ancestor set by walking parent_workflow_id links from the
-- PARENT upward (bounded by max_call_depth), joining tb_mf_workflow_plan for each ancestor's
-- (script_name, plan_version, content_hash) plan-identity key (script_name lives on
-- tb_mf_workflow; no denormalized ancestor-set column anywhere). Rejects call_cycle (the child's
-- own plan-identity key is already in that set) or max_call_depth_exceeded — both a STRUCTURED
-- outcome (matches plan_violation/fence_lost's own category), never a SIGNAL, and always reached
-- with zero writes issued so far.
CREATE PROCEDURE `sp_mf_call_submit`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
	-- Trusted ALREADY CANONICAL (ordered-key compact JSON) — see the header comment. Stored
	-- verbatim as both the op row's input_json AND the child's tb_mf_workflow_args.args_canonical.
	IN arg_input_json mediumtext,
	IN arg_input_hash varchar(64),
	IN arg_new_continuation mediumtext,
	IN arg_event_ts datetime(6),
	IN arg_event_payload mediumtext,
	IN arg_child_workflow_id varbinary(16),
	IN arg_child_script_name varchar(128),
	IN arg_child_plan_version varchar(32),
	IN arg_child_content_hash varbinary(33),
	IN arg_child_plan_length int,
	IN arg_child_continuation mediumtext,
	IN arg_child_next_attempt_at datetime(6),
	IN arg_child_event_payload mediumtext,
	IN arg_parent_node_id varchar(64),
	IN arg_max_call_depth int
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_parent_root_id varbinary(16);
	DECLARE v_parent_call_depth int DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;

	DECLARE v_plan_length int DEFAULT NULL;
	DECLARE v_pred_status tinyint DEFAULT NULL;

	DECLARE v_op_missing tinyint(1) DEFAULT 0;
	DECLARE v_ex_op_id varbinary(16);
	DECLARE v_ex_call_kind tinyint;
	DECLARE v_ex_input_hash varchar(64);
	DECLARE v_call_missing tinyint(1) DEFAULT 0;
	DECLARE v_ex_child_id varbinary(16);
	DECLARE v_ex_child_script varchar(128);
	DECLARE v_ex_child_version varchar(32);
	DECLARE v_ex_child_hash varbinary(33);
	DECLARE v_args_missing tinyint(1) DEFAULT 0;
	DECLARE v_ex_args mediumblob;
	DECLARE v_plan_missing tinyint(1) DEFAULT 0;
	DECLARE v_ex_child_plan_length int;

	DECLARE v_child_depth int;
	DECLARE v_root_workflow_id varbinary(16);
	DECLARE v_cycle_count int DEFAULT 0;

	-- (0) Arg-shape SIGNALs — including the new child inputs, with the SAME entry-check tier
	-- sp_mf_workflow_create_planned already runs for the identical fields (plan_length >= 1,
	-- continuation/event_payload valid JSON OBJECTs, next_attempt_at non-NULL, args a valid JSON
	-- OBJECT) — all before anything else runs.
	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_executor IS NULL OR LENGTH(arg_executor) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfExecutorInvalid';
	END IF;
	IF arg_fencing_token IS NULL OR arg_fencing_token < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfFencingTokenInvalid';
	END IF;
	IF arg_operation_seq IS NULL OR arg_operation_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationSeqInvalid';
	END IF;
	IF arg_operation_id IS NULL OR LENGTH(arg_operation_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationIdInvalid';
	END IF;
	IF arg_input_json IS NULL OR JSON_VALID(arg_input_json) = 0 OR JSON_TYPE(arg_input_json) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfInputJsonInvalid';
	END IF;
	IF arg_input_hash IS NULL OR arg_input_hash = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfInputHashInvalid';
	END IF;
	IF arg_new_continuation IS NULL OR JSON_VALID(arg_new_continuation) = 0 OR JSON_TYPE(arg_new_continuation) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfContinuationInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;
	IF arg_event_payload IS NULL OR JSON_VALID(arg_event_payload) = 0 OR JSON_TYPE(arg_event_payload) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventPayloadInvalid';
	END IF;
	IF arg_child_workflow_id IS NULL OR LENGTH(arg_child_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildWorkflowIdInvalid';
	END IF;
	IF NOT (arg_operation_id <=> arg_child_workflow_id) THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfOperationIdChildMismatch';
	END IF;
	IF arg_child_script_name IS NULL OR arg_child_script_name = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildScriptNameInvalid';
	END IF;
	IF arg_child_plan_version IS NULL OR arg_child_plan_version NOT REGEXP '^[0-9]+\\.[0-9]+\\.[0-9]+$' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildPlanVersionInvalid';
	END IF;
	IF arg_child_content_hash IS NULL OR LENGTH(arg_child_content_hash) <> 33 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildContentHashInvalid';
	END IF;
	IF arg_child_plan_length IS NULL OR arg_child_plan_length < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildPlanLengthInvalid';
	END IF;
	IF arg_child_continuation IS NULL OR JSON_VALID(arg_child_continuation) = 0 OR JSON_TYPE(arg_child_continuation) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildContinuationInvalid';
	END IF;
	IF arg_child_next_attempt_at IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildNextAttemptAtInvalid';
	END IF;
	IF arg_child_event_payload IS NULL OR JSON_VALID(arg_child_event_payload) = 0 OR JSON_TYPE(arg_child_event_payload) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildEventPayloadInvalid';
	END IF;
	IF arg_parent_node_id IS NULL OR arg_parent_node_id = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfParentNodeIdInvalid';
	END IF;
	IF arg_max_call_depth IS NULL OR arg_max_call_depth < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfMaxCallDepthInvalid';
	END IF;

	-- (1) Fence check — load + lock the PARENT row; also read its OWN root_workflow_id/call_depth
	-- here, so the child's ancestry is derived from the DB's own authoritative value, never a
	-- second caller-supplied parameter that could desync from it.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_ts`,
		       `root_workflow_id`, `call_depth`
		INTO v_owner, v_token, v_state, v_event_ts,
		     v_parent_root_id, v_parent_call_depth
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Durable plan ordering (mirrors sp_mf_operation_request exactly): a call op occupies a
	-- call-site position within the PARENT's own plan, same as a participant op, so the same
	-- conformance applies. Enforced BEFORE the existing-row check so a stale out-of-order submit
	-- can't be recorded even as a "replay".
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_plan_length = NULL;
		SELECT `plan_length` INTO v_plan_length FROM `tb_mf_workflow_plan`
		WHERE `workflow_id` = arg_workflow_id;
	END;
	IF v_plan_length IS NOT NULL THEN
		IF arg_operation_seq < 1 OR arg_operation_seq > v_plan_length THEN
			SELECT JSON_OBJECT('outcome', 'plan_violation', 'reason', 'seq_out_of_range',
				'plan_length', CAST(v_plan_length AS SIGNED)) AS result;
			LEAVE proc;
		END IF;
		IF arg_operation_seq > 1 THEN
			BEGIN
				DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_pred_status = NULL;
				SELECT `status` INTO v_pred_status FROM `tb_mf_operation`
				WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq - 1;
			END;
			IF v_pred_status IS NULL OR v_pred_status <> 2 THEN
				SELECT JSON_OBJECT('outcome', 'plan_violation', 'reason', 'predecessor_incomplete',
					'plan_length', CAST(v_plan_length AS SIGNED)) AS result;
				LEAVE proc;
			END IF;
		END IF;
	END IF;

	-- (2) Existing-row check: a prior op row at this call site means this is a replay. Verify
	-- EVERY immutable identity field agrees (round-2/round-4 findings) — not just presence.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_op_missing = 1;
		SELECT `operation_id`, `call_kind`, `input_hash`
		INTO v_ex_op_id, v_ex_call_kind, v_ex_input_hash
		FROM `tb_mf_operation`
		WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
	END;

	IF v_op_missing = 0 THEN
		BEGIN
			DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_call_missing = 1;
			SELECT `child_workflow_id`, `child_script_name`, `child_plan_version`, `child_content_hash`
			INTO v_ex_child_id, v_ex_child_script, v_ex_child_version, v_ex_child_hash
			FROM `tb_mf_call`
			WHERE `workflow_id` = arg_workflow_id AND `operation_seq` = arg_operation_seq;
		END;
		IF v_call_missing = 0 THEN
			BEGIN
				DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_args_missing = 1;
				SELECT `args_canonical` INTO v_ex_args
				FROM `tb_mf_workflow_args`
				WHERE `workflow_id` = v_ex_child_id;
			END;
			-- The durable plan pin is (script_name, plan_version, content_hash, plan_length) —
			-- child_script_name/child_plan_version/child_content_hash are already compared above
			-- (from the tb_mf_call sidecar), but plan_length lives only on tb_mf_workflow_plan and
			-- was previously NOT compared on replay, even though the fresh-submit write phase
			-- writes it from arg_child_plan_length. A missing plan row is a conflict, same as a
			-- missing sidecar/args row.
			BEGIN
				DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_plan_missing = 1;
				SELECT `plan_length` INTO v_ex_child_plan_length
				FROM `tb_mf_workflow_plan`
				WHERE `workflow_id` = v_ex_child_id;
			END;
		END IF;

		IF v_call_missing = 1 OR v_args_missing = 1 OR v_plan_missing = 1
		   OR NOT (v_ex_op_id <=> arg_operation_id
		           AND v_ex_call_kind <=> 2
		           AND v_ex_input_hash <=> arg_input_hash
		           AND v_ex_child_id <=> arg_child_workflow_id
		           AND v_ex_child_script <=> arg_child_script_name
		           AND v_ex_child_version <=> arg_child_plan_version
		           AND v_ex_child_hash <=> arg_child_content_hash
		           AND v_ex_child_plan_length <=> arg_child_plan_length
		           AND v_ex_args <=> arg_input_json) THEN
			SELECT JSON_OBJECT('outcome', 'call_conflict') AS result;
			LEAVE proc;
		END IF;

		SELECT JSON_OBJECT('outcome', 'already_submitted',
			'child_workflow_id', LOWER(HEX(v_ex_child_id))) AS result;
		LEAVE proc;
	END IF;

	-- (3) Recursion guard — ONLY reached on a genuinely fresh submit. Entirely read-only: no
	-- writes issued so far, so a rejection here needs no rollback (the whole point of this
	-- procedure's phasing).
	SET v_child_depth = COALESCE(v_parent_call_depth, 0) + 1;

	IF v_child_depth > arg_max_call_depth THEN
		SELECT JSON_OBJECT('outcome', 'call_rejected', 'reason', 'max_call_depth_exceeded') AS result;
		LEAVE proc;
	END IF;

	WITH RECURSIVE ancestors AS (
		SELECT `workflow_id`, `script_name`, `parent_workflow_id`, 0 AS hops
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		UNION ALL
		SELECT w.`workflow_id`, w.`script_name`, w.`parent_workflow_id`, a.hops + 1
		FROM `tb_mf_workflow` w
		INNER JOIN ancestors a ON w.`workflow_id` = a.`parent_workflow_id`
		WHERE a.hops < arg_max_call_depth
	)
	SELECT COUNT(*) INTO v_cycle_count
	FROM ancestors a
	INNER JOIN `tb_mf_workflow_plan` p ON p.`workflow_id` = a.`workflow_id`
	WHERE a.`script_name` = arg_child_script_name
	  AND p.`plan_version` = arg_child_plan_version
	  AND p.`content_hash` = arg_child_content_hash;

	IF v_cycle_count > 0 THEN
		SELECT JSON_OBJECT('outcome', 'call_rejected', 'reason', 'call_cycle') AS result;
		LEAVE proc;
	END IF;

	-- Time discipline, right before the write phase (a replay above never reaches this — it's not
	-- writing anything new, mirrors sp_mf_operation_request's own ordering).
	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	-- (4) ONLY NOW — every possible rejection already ruled out — the single write phase.
	SET v_root_workflow_id = COALESCE(v_parent_root_id, arg_workflow_id);

	-- Parent-side op row. schema_version=1 is the fixed CALL_OPERATION_SCHEMA_VERSION constant
	-- (never caller-supplied/configurable — NOT the child's plan revision).
	INSERT INTO `tb_mf_operation` (
		`workflow_id`, `operation_seq`, `operation_id`, `operation_name`, `schema_version`,
		`input_json`, `input_hash`, `call_kind`, `status`, `result_json`, `created_at`, `updated_at`
	) VALUES (
		arg_workflow_id, arg_operation_seq, arg_operation_id, arg_child_script_name, 1,
		arg_input_json, arg_input_hash, 2, 1, NULL, arg_event_ts, arg_event_ts
	);

	-- Child: full planned-workflow bundle (workflow + plan + args + its own created event, one
	-- unit — exactly like sp_mf_workflow_create_planned), plus ancestry.
	INSERT INTO `tb_mf_workflow` (
		`workflow_id`, `script_name`, `script_revision`, `state`, `execution_direction`,
		`current_disposition`, `current_event_ts`, `fencing_token`,
		`lease_owner`, `lease_expires_at`, `next_attempt_at`, `current_operation_attempt`,
		`continuation`, `parent_workflow_id`, `parent_node_id`, `root_workflow_id`, `call_depth`,
		`created_at`, `updated_at`
	) VALUES (
		arg_child_workflow_id, arg_child_script_name, 1, 1, 1,
		0, arg_event_ts, 0,
		NULL, NULL, arg_child_next_attempt_at, 0,
		arg_child_continuation, arg_workflow_id, arg_parent_node_id, v_root_workflow_id, v_child_depth,
		arg_event_ts, arg_event_ts
	);

	INSERT INTO `tb_mf_workflow_plan` (`workflow_id`, `plan_version`, `content_hash`, `plan_length`, `created_at`)
	VALUES (arg_child_workflow_id, arg_child_plan_version, arg_child_content_hash, arg_child_plan_length, arg_event_ts);

	INSERT INTO `tb_mf_workflow_args` (`workflow_id`, `args_canonical`, `created_at`)
	VALUES (arg_child_workflow_id, arg_input_json, arg_event_ts);

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_child_workflow_id, arg_event_ts, 'created', NULL, NULL, arg_child_event_payload
	);

	-- Sidecar.
	INSERT INTO `tb_mf_call` (
		`workflow_id`, `operation_seq`, `child_workflow_id`, `child_script_name`,
		`child_plan_version`, `child_content_hash`, `child_status`,
		`first_requested_at`, `last_inspected_at`, `created_at`, `updated_at`
	) VALUES (
		arg_workflow_id, arg_operation_seq, arg_child_workflow_id, arg_child_script_name,
		arg_child_plan_version, arg_child_content_hash, 1,
		arg_event_ts, NULL, arg_event_ts, arg_event_ts
	);

	-- Parent: advance continuation, append its own 'call_submitted' event. Stays forward(1) — the
	-- call operation is now pending, same as an ordinary intermediate operation_request.
	UPDATE `tb_mf_workflow`
	SET `continuation` = arg_new_continuation,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, arg_event_ts, 'call_submitted', arg_executor, NULL, arg_event_payload
	);

	SELECT JSON_OBJECT('outcome', 'submitted', 'child_workflow_id', LOWER(HEX(arg_child_workflow_id))) AS result;
END $$
DELIMITER ;
