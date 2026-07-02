-- Migration 0005 — workflow-to-workflow call runtime spine (composition slice 1b.1)
-- ============================================================================
-- Adds the durable foundation for a `call <child>@<plan_version> { ... }` step: the
-- `call_kind` discriminator on `tb_mf_operation`, ancestry columns on `tb_mf_workflow`, and the
-- new sidecar `tb_mf_call` (work/workflow-composition/DESIGN.md §Durable state).
--
-- Apply ONCE to an existing `microflows` schema; fresh installs get this from the schema/*.sql
-- files directly (no migration needed).
--
-- Backfill rationale: no backfill needed anywhere in this migration. Every PRE-EXISTING
-- `tb_mf_operation` row is, by construction, a participant call (`call_kind` did not exist before
-- this feature, so nothing could have been a child_workflow call) — the column DEFAULT (1) is
-- exactly the correct value for every existing row, with no UPDATE required. Likewise every
-- PRE-EXISTING `tb_mf_workflow` row is top-level (composition did not exist before this feature) —
-- NULL on all four new ancestry columns is exactly correct, satisfying `ck_mf_workflow_ancestry`'s
-- all-NULL branch with zero data changes.
-- ============================================================================

-- 1. tb_mf_operation: add the call_kind discriminator (nullable-shape not needed — DEFAULT 1
--    covers every existing row correctly, see rationale above). No in-place ALTER-CHECK in
--    MariaDB, so the CHECK is a separate statement (matches every other migration in this
--    directory), but unlike the workflow_return_json precedent (0004) there is no backfill step
--    to sequence around — the column and its CHECK can land together in intent, one after another.
ALTER TABLE `tb_mf_operation`
	ADD COLUMN `call_kind` tinyint NOT NULL DEFAULT 1 AFTER `input_hash`;

ALTER TABLE `tb_mf_operation` ADD CONSTRAINT `ck_mf_operation_call_kind` CHECK (`call_kind` IN (1,2));

-- 2. tb_mf_workflow: add the 4 ancestry columns (all nullable; every existing row stays NULL on
--    all four, satisfying the all-or-none CHECK below with no backfill).
ALTER TABLE `tb_mf_workflow`
	ADD COLUMN `parent_workflow_id` varbinary(16) NULL AFTER `workflow_return_json`,
	ADD COLUMN `parent_node_id` varchar(64) NULL AFTER `parent_workflow_id`,
	ADD COLUMN `root_workflow_id` varbinary(16) NULL AFTER `parent_node_id`,
	ADD COLUMN `call_depth` int NULL AFTER `root_workflow_id`;

ALTER TABLE `tb_mf_workflow` ADD CONSTRAINT `ck_mf_workflow_ancestry`
	CHECK (
		(`parent_workflow_id` IS NULL AND `parent_node_id` IS NULL
			AND `root_workflow_id` IS NULL AND `call_depth` IS NULL)
		OR (`parent_workflow_id` IS NOT NULL AND `parent_node_id` IS NOT NULL
			AND `root_workflow_id` IS NOT NULL AND `call_depth` IS NOT NULL AND `call_depth` >= 1)
	);

-- 3. New sidecar tb_mf_call — a brand-new table needs no backfill; CREATE it directly (identical
--    to schema/tb_mf_workflow_operation_call.sql, applied here for an already-deployed schema).
CREATE TABLE IF NOT EXISTS `tb_mf_call` (
	`workflow_id` varbinary(16) NOT NULL,
	`operation_seq` int NOT NULL,
	`child_workflow_id` varbinary(16) NOT NULL,
	`child_script_name` varchar(128) NOT NULL,
	`child_plan_version` varchar(32) NOT NULL,
	`child_content_hash` varbinary(33) NOT NULL,
	`child_status` tinyint NOT NULL DEFAULT 1,
	`first_requested_at` datetime(6) NOT NULL,
	`last_inspected_at` datetime(6) NULL,
	`created_at` datetime(6) NOT NULL,
	`updated_at` datetime(6) NOT NULL,
	PRIMARY KEY (`workflow_id`,`operation_seq`),
	UNIQUE KEY `uq_mf_call_child` (`child_workflow_id`),
	CONSTRAINT `ck_mf_call_status` CHECK (`child_status` IN (1,2,3,4)),
	CONSTRAINT `ck_mf_call_content_hash_len` CHECK (LENGTH(`child_content_hash`) = 33),
	CONSTRAINT `fk_mf_call_operation` FOREIGN KEY (`workflow_id`,`operation_seq`)
		REFERENCES `tb_mf_operation` (`workflow_id`,`operation_seq`),
	CONSTRAINT `fk_mf_call_child` FOREIGN KEY (`child_workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
