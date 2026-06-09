-- Append-only workflow audit trail — the authoritative record (§24.6 D4).
-- tb_mf_workflow denormalizes only current operational fields
-- (current_disposition, current_event_seq, current_event_ts, next_attempt_at).
--
-- Every lifecycle transition and its event commit atomically (one InnoDB
-- transaction with the effects it describes). State transitions are
-- idempotent and deterministic from (current state, stable command input)
-- (§24.4): an already-committed command is resolved by its stable command ID
-- (request_id / invocation ID) BEFORE deriving or appending another event —
-- a replayed command returns its recorded outcome and appends nothing.
--
-- kind is an open string vocabulary while the runtime spine is being built
-- (created, phase_committed, completed, failure_declared, cancelled,
-- reverse_committed, reverse_blocked, resolution, unwound, ...); it hardens
-- to a CHECK once steps 4-6 settle the full set.
CREATE TABLE IF NOT EXISTS `tb_mf_workflow_event` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Workflow-local causal sequence: event_seq = current_event_seq + 1,
	-- derived INSIDE the fenced publication transaction (§24.4). No
	-- AUTO_INCREMENT: identifiers are deterministic, so replaying the same
	-- command stream against the same initial state produces identical history.
	`event_seq` bigint NOT NULL,
	-- Caller-supplied audit/scheduling value (§24.4): sourced by the runtime
	-- (database clock in production, controlled clock in tests), FIXED across
	-- retries of the same command, stored unchanged. Causal ordering comes
	-- from event_seq, never from timestamps; SQL never adjusts a value.
	`event_ts` datetime(6) NOT NULL,
	`kind` varchar(40) NOT NULL,
	-- Executor / operator / service identity (16 bytes, same convention as
	-- lease_owner). NULL for runtime-internal events.
	`actor` varbinary(16) NULL,
	-- Stable command ID for interventions (cancel/resolution) and other
	-- externally issued commands; NULL for ordinary runtime events.
	`request_id` varbinary(16) NULL,
	-- JSON DOCUMENT: non-NULL, valid, OBJECT ("{}" when there is nothing to say).
	`payload` mediumtext NOT NULL CHECK (json_valid(`payload`) AND json_type(`payload`) = 'OBJECT'),
	PRIMARY KEY (`workflow_id`,`event_seq`),
	UNIQUE KEY `uq_mf_event_request` (`workflow_id`,`request_id`),
	CONSTRAINT `fk_mf_event_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
