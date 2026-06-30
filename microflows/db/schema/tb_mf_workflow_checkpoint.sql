-- Checkpoint stack — the durable results of committed Phases, in reversal
-- order (§4.1, §6, §7.1).
--
-- A Checkpoint row is created in the SAME transaction as its Phase's effects
-- (§19.3: no effects without Checkpoint, no Checkpoint without effects). The
-- row is also the §14 idempotency marker: (workflow_id, operation_id)
-- proves an invocation committed, so a retry discovers the commit instead of
-- applying again.
--
-- Checkpoints hold NO runtime resources (§19.8): no lock, lease, or open
-- guard survives a Phase boundary. Inter-Phase protection is durable domain
-- state (reservations, drawers).
--
-- reversal_state codes:
--   1 = active                (committed; will reverse if the workflow backs out)
--   2 = reversed              (reverse committed)
--   3 = resolution_required   (reverse failed nonretryably; unwind blocked, §7.1)
--   4 = resolved              (authorized resolution disposed of it)
-- The design doc's transient "reversing" is an in-flight executor condition
-- under a lease — analogous to "running" for workflows — and is never durable.
CREATE TABLE IF NOT EXISTS `tb_mf_workflow_checkpoint` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Stack position, 1-based; reversal proceeds from the highest active seq
	-- downward.
	`seq` int NOT NULL,
	`operation_name` varchar(128) NOT NULL,
	-- Stable phase invocation ID (§14.1): constant across transient retries of
	-- one logical invocation.
	`operation_id` varbinary(16) NOT NULL,
	-- The Checkpoint's typed result payload. JSON DOCUMENT: non-NULL, valid,
	-- OBJECT. Recovery must not substitute a fresh read of mutable data (§4.1).
	`payload` mediumtext NOT NULL CHECK (json_valid(`payload`) AND json_type(`payload`) = 'OBJECT'),
	`reversal_state` tinyint NOT NULL DEFAULT 1,
	-- Durable REVERSE-operation binding, persisted by reverse_request BEFORE the
	-- compensation is dispatched (§6). This pins the checkpoint to the exact
	-- compensation CONTRACT (operation name + schema version) and the exact
	-- input/payload identity it was bound to, so a later registry/manual-IR change
	-- cannot resume an in-flight unwind through a different contract. On recovery
	-- the pinned (reverse_operation_name, reverse_schema_version) is resolved
	-- exactly like a forward pinned operation. All NULL until reverse_request.
	-- Stable reverse invocation ID once reversal of this checkpoint begins.
	`reverse_invocation_id` varbinary(16) NULL,
	`reverse_operation_name` varchar(128) NULL,
	`reverse_schema_version` int NULL,
	`reverse_input_json` mediumtext NULL CHECK (`reverse_input_json` IS NULL OR (json_valid(`reverse_input_json`) AND json_type(`reverse_input_json`) = 'OBJECT')),
	`reverse_input_hash` varchar(64) NULL,
	`reversed_at` datetime(6) NULL,
	-- event_seq of the event that disposed of this checkpoint when reversal
	-- needed resolution.
	`resolution_event_seq` bigint NULL,
	`created_at` datetime(6) NOT NULL,
	`updated_at` datetime(6) NOT NULL,
	-- Durable bounded reconcile budget for a persistent route-404 on THIS checkpoint's compensation
	-- dispatch (#2, reverse side). Advanced ONLY by the reverse reconcile-defer SP on a confirmed Route404,
	-- keyed by (workflow_id, seq) so resume never resets it. NULL/0 until the first route-404.
	`reconcile_attempts` int NOT NULL DEFAULT 0,
	`reconcile_first_seen_at` datetime(6) NULL,
	`reconcile_last_seen_at` datetime(6) NULL,
	`reconcile_reason` varchar(64) NULL,
	-- Durable pending->re-dispatch escalation timer for a compensation whose participant committed and
	-- crashed before Singular.complete (Phase 7 case [12], reverse side). Advanced ONLY by
	-- sp_mf_checkpoint_pending_defer on a CONFIRMED participant pending of a RECOVERED reverse dispatch.
	-- Keyed by (workflow_id, seq) so resume never resets it. Same discipline as the forward timer.
	`redispatch_first_seen_at` datetime(6) NULL,
	`redispatch_last_at` datetime(6) NULL,
	`redispatch_count` int NOT NULL DEFAULT 0,
	PRIMARY KEY (`workflow_id`,`seq`),
	UNIQUE KEY `uq_mf_checkpoint_invocation` (`workflow_id`,`operation_id`),
	CONSTRAINT `ck_mf_checkpoint_reversal_state` CHECK (`reversal_state` IN (1,2,3,4)),
	-- The reverse binding is an ALL-OR-NONE tuple: either no compensation has been
	-- dispatched (all five fields NULL) or the full durable binding is present AND
	-- VALID. Prevents an invocation id without its pinned contract/input (which
	-- reverse_head would mis-classify as 'dispatched') and a present-but-degenerate
	-- binding (empty name/hash, schema_version < 1, wrong-length id). The
	-- reverse_input_json OBJECT validity is enforced by its own column CHECK.
	CONSTRAINT `ck_mf_checkpoint_reverse_binding` CHECK (
		(`reverse_invocation_id` IS NULL AND `reverse_operation_name` IS NULL
		 AND `reverse_schema_version` IS NULL AND `reverse_input_json` IS NULL
		 AND `reverse_input_hash` IS NULL)
		OR
		(`reverse_invocation_id` IS NOT NULL AND LENGTH(`reverse_invocation_id`) = 16
		 AND `reverse_operation_name` IS NOT NULL AND `reverse_operation_name` <> ''
		 AND `reverse_schema_version` IS NOT NULL AND `reverse_schema_version` >= 1
		 AND `reverse_input_json` IS NOT NULL
		 AND `reverse_input_hash` IS NOT NULL AND `reverse_input_hash` <> '')
	),
	CONSTRAINT `fk_mf_checkpoint_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
