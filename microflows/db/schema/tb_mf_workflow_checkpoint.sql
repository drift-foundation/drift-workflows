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
	-- Stable reverse invocation ID once reversal of this checkpoint begins.
	`reverse_invocation_id` varbinary(16) NULL,
	`reversed_at` datetime(6) NULL,
	-- event_seq of the event that disposed of this checkpoint when reversal
	-- needed resolution.
	`resolution_event_seq` bigint NULL,
	`created_at` datetime(6) NOT NULL,
	`updated_at` datetime(6) NOT NULL,
	PRIMARY KEY (`workflow_id`,`seq`),
	UNIQUE KEY `uq_mf_checkpoint_invocation` (`workflow_id`,`operation_id`),
	CONSTRAINT `ck_mf_checkpoint_reversal_state` CHECK (`reversal_state` IN (1,2,3,4)),
	CONSTRAINT `fk_mf_checkpoint_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
