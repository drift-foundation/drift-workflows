DELIMITER $$
-- Durable OPERATIONAL deferral of a dispatch (microflows_design.md §3.1): the
-- operation cannot be dispatched right now for a REPAIRABLE reason — e.g. the
-- pinned participant/operation binding is temporarily unavailable in trusted
-- config (pinned_contract_unavailable), or a concurrently-submitted request is
-- not yet persisted (operation_request_absent). This is NOT a participant
-- failure and NOT blocked_resolution: the workflow stays FORWARD and claimable so
-- that restoring configuration (or the missing request) lets it continue
-- automatically on the next poll.
--
-- It is purely a scheduling/lease action plus an audit note: state, disposition,
-- continuation, and the operation row are ALL left unchanged; only the lease is
-- cleared and next_attempt_at is set to a caller-supplied absolute future time.
--
-- Fenced like every publication (owner + token + forward state). Distinct from
-- sp_mf_workflow_release (lease-only, no audit) and sp_mf_operation_fail
-- (participant failure -> blocked_resolution). To avoid emitting identical events
-- on every retry, the 'operation_dispatch_deferred' audit event is appended only
-- on ENTRY into the wait condition: if the latest event is already this kind with
-- the same reason, the deferral repeats without a new event (outcome carries
-- `appended`).
CREATE PROCEDURE `sp_mf_operation_dispatch_defer`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_db_now datetime(6),
	IN arg_next_attempt_at datetime(6),
	IN arg_event_ts datetime(6),
	IN arg_reason varchar(64)
)
proc:BEGIN
	DECLARE v_owner varbinary(16);
	DECLARE v_token bigint;
	DECLARE v_state tinyint;
	DECLARE v_event_seq bigint;
	DECLARE v_event_ts datetime(6);
	DECLARE v_last_kind varchar(40) DEFAULT NULL;
	DECLARE v_last_reason varchar(64) DEFAULT NULL;
	DECLARE v_new_event_seq bigint;
	DECLARE v_new_event_ts datetime(6);
	DECLARE v_append tinyint(1) DEFAULT 1;
	DECLARE v_missing tinyint(1) DEFAULT 0;
	DECLARE v_no_event tinyint(1) DEFAULT 0;

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
	IF arg_event_ts IS NULL THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfEventTsInvalid';
	END IF;
	IF arg_reason IS NULL OR arg_reason = '' THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfReasonInvalid';
	END IF;
	-- A deferral is a FUTURE backoff: the next attempt must be strictly later than
	-- now, or a malformed caller could schedule immediate/past reclaim and spin a
	-- hot retry loop on the shared instance.
	IF arg_next_attempt_at <= arg_db_now THEN
		SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfNextAttemptNotFuture';
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

	-- Fence: must hold the lease on a FORWARD workflow (deferral is a forward-path
	-- scheduling action, never a transition out of forward).
	IF v_owner IS NULL OR v_owner <> arg_executor OR v_token <> arg_fencing_token OR v_state <> 1 THEN
		SELECT JSON_OBJECT('outcome', 'fence_lost') AS result;
		LEAVE proc;
	END IF;

	-- Dedup: append the audit event only on ENTRY into this wait condition.
	BEGIN
		DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_no_event = 1;
		SELECT `kind`, JSON_UNQUOTE(JSON_EXTRACT(`payload`, '$.reason'))
		INTO v_last_kind, v_last_reason
		FROM `tb_mf_workflow_event`
		WHERE `workflow_id` = arg_workflow_id
		ORDER BY `event_seq` DESC
		LIMIT 1;
	END;

	IF v_no_event = 0 AND v_last_kind = 'operation_dispatch_deferred' AND (v_last_reason <=> arg_reason) THEN
		SET v_append = 0;
	END IF;
	-- A non-increasing event_ts (clock skew) cannot order a new event; fold into
	-- the no-append path rather than fail — the deferral itself still proceeds.
	IF v_append = 1 AND arg_event_ts <= v_event_ts THEN
		SET v_append = 0;
	END IF;

	SET v_new_event_seq = v_event_seq;
	SET v_new_event_ts = v_event_ts;
	IF v_append = 1 THEN
		SET v_new_event_seq = v_event_seq + 1;
		SET v_new_event_ts = arg_event_ts;
	END IF;

	-- Lease axis + scheduling only; state(1)/disposition/continuation unchanged.
	UPDATE `tb_mf_workflow`
	SET `lease_owner` = NULL,
	    `lease_expires_at` = NULL,
	    `next_attempt_at` = arg_next_attempt_at,
	    `current_event_seq` = v_new_event_seq,
	    `current_event_ts` = v_new_event_ts,
	    `updated_at` = arg_db_now
	WHERE `workflow_id` = arg_workflow_id;

	IF v_append = 1 THEN
		INSERT INTO `tb_mf_workflow_event` (
			`workflow_id`, `event_seq`, `event_ts`, `kind`, `actor`, `request_id`, `payload`
		) VALUES (
			arg_workflow_id, v_new_event_seq, arg_event_ts, 'operation_dispatch_deferred',
			arg_executor, NULL, JSON_OBJECT('reason', arg_reason)
		);
	END IF;

	SELECT JSON_OBJECT('outcome', 'deferred', 'appended', v_append) AS result;
END $$
DELIMITER ;
