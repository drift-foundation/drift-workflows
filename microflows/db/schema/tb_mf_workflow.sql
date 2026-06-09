-- Workflow instance — one row per workflow execution (design doc §24, §24.6).
--
-- Three deliberately separate axes (§24.1):
--   lifecycle state  (state)                       — what the workflow is
--   lease ownership  (lease_owner/expires/fencing) — who may publish for it
--   scheduling       (next_attempt_at)             — when it next runs
--
-- "Running" is NOT a state: it is a claimable state holding a valid lease.
-- Recovery is an expired lease on a claimable state, not a transition.
--
-- state codes (must stay in sync with packages/microflows/src/state.drift):
--   1=forward 2=reversing 3=blocked_resolution
--   4=completed 5=reversed 6=resolved_exception
--   (reversed = genuine full compensation; resolved_exception = audited
--    accepted exception WITHOUT full compensation — never conflate them)
-- execution_direction codes (the mode; retained across a block):
--   1=forward 2=reverse
-- current_disposition codes:
--   0=none 1=completed 2=failed 3=cancelled
--   4=indeterminate (a forward op is indeterminate; blocked awaiting resolution)
--
-- The claimable predicate (§24.1):
--   state IN (1,2) AND next_attempt_at <= :db_now
--   AND (lease_owner IS NULL OR lease_expires_at < :db_now)
--
-- TIME/ORDERING/COMMAND DISCIPLINE (§24.4): the database clock is the time
-- AUTHORITY but SQL is never the time GENERATOR, and timestamps never carry
-- ordering. No column has a DEFAULT/ON UPDATE timestamp and nothing is
-- AUTO_INCREMENT; the runtime supplies every timestamp (event_ts,
-- lease_expires_at, next_attempt_at, created_at, updated_at) as an explicit
-- command parameter, FIXED across retries of the same command, stored
-- unchanged. Causal ordering is current_event_seq; transitions are
-- idempotent and deterministic from (current state, stable command input),
-- and an already-committed command is resolved by its stable command ID
-- before any new event is derived or appended.
--
-- Every publication (phase commit, reverse commit, completion, backoff,
-- transition) re-validates workflow_id + lease_owner + fencing_token +
-- unexpired lease + expected state INSIDE its transaction (§24.3), and
-- derives event_seq = current_event_seq + 1 there.
CREATE TABLE IF NOT EXISTS `tb_mf_workflow` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Pinned immutable script revision (§22). Milestone 1: resolved against the
	-- executor's in-process registry of manually constructed IR (no script
	-- table yet; it arrives with the parser).
	`script_name` varchar(128) NOT NULL,
	`script_revision` int NOT NULL,
	`state` tinyint NOT NULL,
	-- Execution direction (the mode the workflow is in): 1=forward, 2=reverse.
	-- Forward while state=forward; reverse while state=reversing; RETAINED while
	-- blocked_resolution so the valid authorized-resolution transitions are
	-- known. Mirrors microflows.state ExecutionDirection.
	`execution_direction` tinyint NOT NULL DEFAULT 1,
	`current_disposition` tinyint NOT NULL DEFAULT 0,
	-- Workflow-local causal sequence projection (D4, §24.4): event_seq of the
	-- latest tb_mf_workflow_event row; 0 before the first event. Appends
	-- derive event_seq = current_event_seq + 1 inside the fenced transaction.
	`current_event_seq` bigint NOT NULL DEFAULT 0,
	-- event_ts of the latest event (audit/scheduling projection; supplied
	-- value, unchanged, fixed across retries). Never used for ordering.
	`current_event_ts` datetime(6) NOT NULL,
	-- Fencing token: bumped on every claim and on direct intervention
	-- transitions (cancellation). A stale holder can compute but cannot publish.
	`fencing_token` bigint NOT NULL DEFAULT 0,
	`lease_owner` varbinary(16) NULL,
	`lease_expires_at` datetime(6) NULL,
	`next_attempt_at` datetime(6) NOT NULL,
	-- Transient-retry counter for the phase invocation currently being executed
	-- (§14.1 execution_attempt). Reset at each phase commit.
	`current_operation_attempt` int NOT NULL DEFAULT 0,
	-- Durable continuation (§4.1): next executable position in the pinned IR +
	-- ordinary typed locals that survive the boundary. JSON DOCUMENT: non-NULL,
	-- valid, OBJECT (the empty document is `{}`).
	`continuation` mediumtext NOT NULL CHECK (json_valid(`continuation`) AND json_type(`continuation`) = 'OBJECT'),
	`created_at` datetime(6) NOT NULL,
	`updated_at` datetime(6) NOT NULL,
	PRIMARY KEY (`workflow_id`),
	-- Claim scan (§24.2): WHERE script_name = ? AND state IN (1,2)
	-- AND next_attempt_at <= :db_now ... ORDER BY next_attempt_at, workflow_id.
	-- Script-scoped: an executor claims only scripts in its IR registry.
	KEY `idx_mf_workflow_claim` (`script_name`,`state`,`next_attempt_at`),
	CONSTRAINT `ck_mf_workflow_state` CHECK (`state` IN (1,2,3,4,5,6)),
	CONSTRAINT `ck_mf_workflow_direction` CHECK (`execution_direction` IN (1,2)),
	CONSTRAINT `ck_mf_workflow_disposition` CHECK (`current_disposition` IN (0,1,2,3,4)),
	-- (state, disposition) representability (mirror of disposition_valid_for in
	-- state.drift): forward=none; reversing=failed|cancelled; blocked carries the
	-- reversal cause (failed|cancelled) OR a forward indeterminate; completed=
	-- completed; reversed=failed|cancelled; resolved_exception keeps the
	-- underlying cause (failed|cancelled|indeterminate).
	CONSTRAINT `ck_mf_workflow_state_disposition` CHECK ((`state` = 1 AND `current_disposition` = 0) OR (`state` = 2 AND `current_disposition` IN (2,3)) OR (`state` = 3 AND `current_disposition` IN (2,3,4)) OR (`state` = 4 AND `current_disposition` = 1) OR (`state` = 5 AND `current_disposition` IN (2,3)) OR (`state` = 6 AND `current_disposition` IN (2,3,4))),
	-- (state, direction) consistency invariant (mirror of state_direction_valid
	-- in state.drift). forward=forward, reversing=reverse, blocked=either,
	-- completed=forward, reversed=reverse, resolved_exception=either.
	CONSTRAINT `ck_mf_workflow_state_direction` CHECK ((`state` = 1 AND `execution_direction` = 1) OR (`state` = 2 AND `execution_direction` = 2) OR (`state` = 3 AND `execution_direction` IN (1,2)) OR (`state` = 4 AND `execution_direction` = 1) OR (`state` = 5 AND `execution_direction` = 2) OR (`state` = 6 AND `execution_direction` IN (1,2))),
	-- A lease is either fully present or fully absent.
	CONSTRAINT `ck_mf_workflow_lease_pair` CHECK (
		(`lease_owner` IS NULL AND `lease_expires_at` IS NULL)
		OR (`lease_owner` IS NOT NULL AND `lease_expires_at` IS NOT NULL)
	)
) ENGINE=InnoDB;
