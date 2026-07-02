DELIMITER $$
-- Composition (1b.1): WAKE + STATUS-HINT ONLY (§Liveness) — invoked from the CHILD's own
-- terminal-transition context (a SEPARATE call, strictly AFTER that transaction has already
-- committed; never nested inside it — nesting would lock two different workflows' rows in one
-- transaction, an unproven cross-workflow lock-ordering risk this system has never needed
-- elsewhere). Deliberately best-effort and UNFENCED: this call site holds no lease/fencing_token
-- on the PARENT (it has no reason to — it is driven by the CHILD's own terminal event, a
-- different workflow entirely), and correctness never depends on it landing (sp_mf_call_inspect's
-- poll is the correctness floor; a missed/duplicate/racy notify only changes how soon the parent
-- wakes up, never whether it eventually does).
--
-- Touches EXACTLY two things: the sidecar's child_status hint (via child_workflow_id, monotonic
-- by time, same no-clobber-on-stale-poll rule as sp_mf_call_hint_refresh) and the PARENT's
-- next_attempt_at, PULLED EARLIER ONLY (LEAST(current, arg_event_ts) — a wake may accelerate the
-- parent's next scan, never delay it). Never touches tb_mf_operation, never creates a checkpoint,
-- never appends a tb_mf_workflow_event row (event_seq derivation is a FENCED-transaction-only
-- discipline; this unfenced call must not participate in it), never sets the parent's
-- status/result_json/state/lease. The runner (via sp_mf_call_inspect) remains the single
-- authority that actually settles or reverses the parent's call operation.
--
-- Outcomes:
--   notified  — hint (best-effort) + wake applied (or both were no-ops: stale poll / already due)
--   not_found — no tb_mf_call row for this child_workflow_id (not a live call operation)
CREATE PROCEDURE `sp_mf_child_terminal_notify`(
	IN arg_child_workflow_id varbinary(16),
	IN arg_child_status tinyint,
	IN arg_event_ts datetime(6)
)
proc:BEGIN
	DECLARE v_workflow_id varbinary(16);
	DECLARE v_operation_seq int;
	DECLARE v_last_inspected datetime(6) DEFAULT NULL;
	DECLARE v_missing tinyint(1) DEFAULT 0;

	IF arg_child_workflow_id IS NULL OR LENGTH(arg_child_workflow_id) <> 16 THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildWorkflowIdInvalid';
	END IF;
	-- Terminal-only (§Liveness): this procedure is invoked on a child TERMINAL, so `blocked`
	-- (code 4, non-terminal) is out of scope here — that hint refresh is sp_mf_call_hint_refresh's
	-- job. (child_status codes: 1=pending 2=completed 3=failed 4=blocked.)
	IF arg_child_status IS NULL OR arg_child_status NOT IN (2,3) THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfChildStatusInvalid';
	END IF;
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;

	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_missing = 1;
		SELECT `workflow_id`, `operation_seq`, `last_inspected_at`
		INTO v_workflow_id, v_operation_seq, v_last_inspected
		FROM `tb_mf_call`
		WHERE `child_workflow_id` = arg_child_workflow_id
		FOR UPDATE;
	END;

	IF v_missing = 1 THEN
		SELECT JSON_OBJECT('outcome', 'not_found') AS result;
		LEAVE proc;
	END IF;

	IF v_last_inspected IS NULL OR arg_event_ts > v_last_inspected THEN
		UPDATE `tb_mf_call`
		SET `child_status` = arg_child_status,
		    `last_inspected_at` = arg_event_ts,
		    `updated_at` = arg_event_ts
		WHERE `workflow_id` = v_workflow_id AND `operation_seq` = v_operation_seq;
	END IF;

	-- Wake: pull the parent's next scan earlier, never later. No fence, no event, no other column.
	UPDATE `tb_mf_workflow`
	SET `next_attempt_at` = LEAST(`next_attempt_at`, arg_event_ts)
	WHERE `workflow_id` = v_workflow_id;

	SELECT JSON_OBJECT('outcome', 'notified') AS result;
END $$
DELIMITER ;
