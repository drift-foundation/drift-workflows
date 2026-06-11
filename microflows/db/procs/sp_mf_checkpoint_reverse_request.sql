DELIMITER $$
-- Persist a checkpoint's REVERSE-invocation id BEFORE the compensation is
-- dispatched (microflows_design.md §6, §14): the durable marker that proves which
-- reverse op was sent, so a lost ack reconciles by GET on this stable id instead
-- of re-dispatching blindly. Crash-safe + idempotent: the reverse id is derived
-- deterministically by the runner from (workflow, seq), so on recovery this proc
-- finds it already set and returns it (reconcile path) rather than a second id.
--
-- The transition layer ENFORCES reverse order: the target seq must be the current
-- TOP (highest-seq) active checkpoint, so a runner bug cannot compensate out of
-- order. Fenced on REVERSING(2); time-disciplined (arg_event_ts strictly after the
-- current causal timestamp). The already-requested replay is lease-independent.
CREATE PROCEDURE `sp_mf_checkpoint_reverse_request`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_seq int,
	IN arg_reverse_id varbinary(16),
	IN arg_reverse_operation_name varchar(128),
	IN arg_reverse_schema_version int,
	IN arg_reverse_input_json mediumtext,
	IN arg_reverse_input_hash varchar(64),
	IN arg_event_ts datetime(6)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_cp_state tinyint;
	DECLARE v_cp_revid varbinary(16);
	DECLARE v_cp_rname varchar(128);
	DECLARE v_cp_rsv int;
	DECLARE v_cp_rinput mediumtext;
	DECLARE v_cp_rhash varchar(64);
	DECLARE v_top_seq int DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_cp_missing tinyint(1) DEFAULT 0;

	IF arg_workflow_id IS NULL OR LENGTH(arg_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowIdInvalid';
	END IF;
	IF arg_executor IS NULL OR LENGTH(arg_executor) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfExecutorInvalid';
	END IF;
	IF arg_fencing_token IS NULL OR arg_fencing_token < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfFencingTokenInvalid';
	END IF;
	IF arg_seq IS NULL OR arg_seq < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfSeqInvalid';
	END IF;
	IF arg_reverse_id IS NULL OR LENGTH(arg_reverse_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReverseIdInvalid';
	END IF;
	-- Durable compensation binding (pinned contract + input identity).
	IF arg_reverse_operation_name IS NULL OR arg_reverse_operation_name = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReverseOpNameInvalid';
	END IF;
	IF arg_reverse_schema_version IS NULL OR arg_reverse_schema_version < 1 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReverseSchemaVersionInvalid';
	END IF;
	IF arg_reverse_input_json IS NULL OR NOT JSON_VALID(arg_reverse_input_json) OR JSON_TYPE(arg_reverse_input_json) <> 'OBJECT' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReverseInputJsonInvalid';
	END IF;
	IF arg_reverse_input_hash IS NULL OR arg_reverse_input_hash = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReverseInputHashInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `lease_owner`, `fencing_token`, `state`, `current_event_seq`, `current_event_ts`
		INTO v_owner, v_token, v_state, v_event_seq, v_event_ts
		FROM `tb_mf_workflow`
		WHERE `workflow_id` = arg_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_cp_missing = 1;
		SELECT `reversal_state`, `reverse_invocation_id`, `reverse_operation_name`,
		       `reverse_schema_version`, `reverse_input_json`, `reverse_input_hash`
		INTO v_cp_state, v_cp_revid, v_cp_rname, v_cp_rsv, v_cp_rinput, v_cp_rhash
		FROM `tb_mf_workflow_checkpoint`
		WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq
		FOR UPDATE;
	END;

	IF v_cp_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_not_found') AS result;
		LEAVE proc;
	END IF;

	-- Idempotent replays (lease-independent): already past active, or already
	-- dispatched (recovery).
	IF v_cp_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'checkpoint_inactive', 'reversal_state', CAST(v_cp_state AS SIGNED)) AS result;
		LEAVE proc;
	END IF;
	IF v_cp_revid IS NOT NULL THEN
		-- Binding is IMMUTABLE once persisted (like operation_request's identity):
		-- the supplied binding must match the persisted one, else a racing runner
		-- with a re-derived binding (e.g. registry/manual-IR changed) would dispatch
		-- the wrong contract under the persisted id. The input is compared by its
		-- actual JSON CONTENT (the caller-supplied hash is not DB-verified, so a
		-- right-hash/wrong-json pair must not slip through). The persisted input is
		-- returned so the runner dispatches THAT durable value, never its own.
		IF NOT (v_cp_revid <=> arg_reverse_id)
		   OR NOT (v_cp_rname <=> arg_reverse_operation_name)
		   OR NOT (v_cp_rsv <=> arg_reverse_schema_version)
		   OR NOT (v_cp_rhash <=> arg_reverse_input_hash)
		   OR NOT (v_cp_rinput <=> arg_reverse_input_json) THEN
			SELECT JSON_OBJECT('outcome', 'binding_conflict') AS result;
			LEAVE proc;
		END IF;
		SELECT JSON_OBJECT('outcome', 'already_requested',
			'reverse_invocation_id', LOWER(HEX(v_cp_revid)),
			'reverse_operation_name', v_cp_rname,
			'reverse_schema_version', CAST(v_cp_rsv AS SIGNED),
			'reverse_input_json', JSON_EXTRACT(v_cp_rinput, '$'),
			'reverse_input_hash', v_cp_rhash) AS result;
		LEAVE proc;
	END IF;

	-- Fence: lease holder on a REVERSING workflow.
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 2 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Reverse ORDER: the target must be the current top (highest-seq) active
	-- checkpoint, so compensation cannot run out of stack order.
	SELECT MAX(`seq`) INTO v_top_seq
	FROM `tb_mf_workflow_checkpoint`
	WHERE `workflow_id` = arg_workflow_id AND `reversal_state` = 1;
	IF arg_seq <> v_top_seq THEN
		SELECT JSON_OBJECT('outcome', 'out_of_order', 'top_seq', CAST(v_top_seq AS SIGNED)) AS result;
		LEAVE proc;
	END IF;

	-- Time discipline: a new causal event must move the timestamp forward.
	IF arg_event_ts <= v_event_ts THEN
		SELECT JSON_OBJECT('outcome', 'event_time_skew',
			'defer_until', DATE_FORMAT(v_event_ts + INTERVAL 5 SECOND, '%Y-%m-%d %H:%i:%s.%f')) AS result;
		LEAVE proc;
	END IF;

	-- First dispatch of this compensation: persist the reverse id + cursor + audit.
	SET v_event_seq = v_event_seq + 1;

	UPDATE `tb_mf_workflow_checkpoint`
	SET `reverse_invocation_id` = arg_reverse_id,
	    `reverse_operation_name` = arg_reverse_operation_name,
	    `reverse_schema_version` = arg_reverse_schema_version,
	    `reverse_input_json` = arg_reverse_input_json,
	    `reverse_input_hash` = arg_reverse_input_hash,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id AND `seq` = arg_seq;

	UPDATE `tb_mf_workflow`
	SET `continuation` = JSON_OBJECT('pos', 'reverse:dispatched', 'seq', arg_seq),
	    `current_event_seq` = v_event_seq,
	    `current_event_ts` = arg_event_ts,
	    `updated_at` = arg_event_ts
	WHERE `workflow_id` = arg_workflow_id;

	INSERT INTO `tb_mf_workflow_event` (
		`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
	) VALUES (
		arg_workflow_id, v_event_seq, arg_event_ts, 'compensation_requested', arg_executor, NULL,
		JSON_OBJECT('seq', arg_seq, 'reverse_invocation_id', LOWER(HEX(arg_reverse_id)),
			'reverse_operation', arg_reverse_operation_name, 'reverse_schema_version', arg_reverse_schema_version)
	);

	SELECT JSON_OBJECT('outcome', 'requested',
		'reverse_invocation_id', LOWER(HEX(arg_reverse_id))) AS result;
END $$
DELIMITER ;
