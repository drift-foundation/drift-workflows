-- Migration 0003 — durable pending→re-dispatch escalation timer (Phase 7 case [12])
-- ============================================================================
-- Adds the per-dispatch pending-redispatch timer columns to the FORWARD operation
-- row (tb_mf_operation) and the REVERSE checkpoint row (tb_mf_workflow_checkpoint),
-- parallel to the #2 reconcile-budget columns. Advanced ONLY by the pending-defer
-- SPs on a CONFIRMED participant pending (GET 202 / PendingObserved) of a RECOVERED
-- dispatch — never on a fresh dispatch, a 5xx, or transport failure. Keyed by the
-- existing primary key so a resume re-reads the same row and the escalation epoch
-- cannot reset. All NULL/0 by default -> existing rows are timer-unused; online-safe.
-- Fresh installs get these from the schema files directly (no migration needed).
--
-- Unlike the #2 budget there is NO exhaustion/block: a re-PUT is idempotent and safe,
-- so pending→re-dispatch escalates indefinitely (a genuinely broken op fails
-- definitively via the rerun's 400 -> reversal; a slow-but-alive op keeps answering
-- 202). first_seen anchors the epoch once; last_at re-arms after each escalation.
-- ============================================================================

ALTER TABLE `tb_mf_operation`
	ADD COLUMN `redispatch_first_seen_at` datetime(6) NULL AFTER `reconcile_reason`,
	ADD COLUMN `redispatch_last_at` datetime(6) NULL AFTER `redispatch_first_seen_at`,
	ADD COLUMN `redispatch_count` int NOT NULL DEFAULT 0 AFTER `redispatch_last_at`;

ALTER TABLE `tb_mf_workflow_checkpoint`
	ADD COLUMN `redispatch_first_seen_at` datetime(6) NULL AFTER `reconcile_reason`,
	ADD COLUMN `redispatch_last_at` datetime(6) NULL AFTER `redispatch_first_seen_at`,
	ADD COLUMN `redispatch_count` int NOT NULL DEFAULT 0 AFTER `redispatch_last_at`;
