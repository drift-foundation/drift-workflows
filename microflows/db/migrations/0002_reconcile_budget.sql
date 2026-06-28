-- Migration 0002 — durable bounded reconcile budget for persistent participant route-404s (#2)
-- ============================================================================
-- Adds the per-dispatch reconcile-budget columns to the FORWARD operation row
-- (tb_mf_operation) and the REVERSE checkpoint row (tb_mf_workflow_checkpoint).
-- Advanced ONLY by the reconcile-defer SPs on a confirmed Route404, keyed by the
-- existing primary key so a resume re-reads the same row and the budget cannot
-- reset. All NULL/0 by default -> existing rows are budget-unused; online-safe.
-- Fresh installs get these from the schema files directly (no migration needed).
-- ============================================================================

ALTER TABLE `tb_mf_operation`
	ADD COLUMN `reconcile_attempts` int NOT NULL DEFAULT 0 AFTER `updated_at`,
	ADD COLUMN `reconcile_first_seen_at` datetime(6) NULL AFTER `reconcile_attempts`,
	ADD COLUMN `reconcile_last_seen_at` datetime(6) NULL AFTER `reconcile_first_seen_at`,
	ADD COLUMN `reconcile_reason` varchar(64) NULL AFTER `reconcile_last_seen_at`;

ALTER TABLE `tb_mf_workflow_checkpoint`
	ADD COLUMN `reconcile_attempts` int NOT NULL DEFAULT 0 AFTER `updated_at`,
	ADD COLUMN `reconcile_first_seen_at` datetime(6) NULL AFTER `reconcile_attempts`,
	ADD COLUMN `reconcile_last_seen_at` datetime(6) NULL AFTER `reconcile_first_seen_at`,
	ADD COLUMN `reconcile_reason` varchar(64) NULL AFTER `reconcile_last_seen_at`;
