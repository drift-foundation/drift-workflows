-- Durable PIN of a workflow's manual-IR forward plan (§22, milestone-1 manual IR).
--
-- The forward plan (the ordered operation steps) lives in mutable executor config.
-- This row pins it to the workflow at creation so a later config change cannot
-- silently alter an in-flight workflow: reorder/shorten/change a step, or move
-- finality. A resuming worker re-derives the plan hash from its config and the pin
-- proc accepts it only if it MATCHES the committed pin (else plan_conflict).
--
-- `plan_length` makes operation finality DURABLE: operation_settle derives whether
-- a step is the last one (seq == plan_length) instead of trusting the caller, so a
-- runner defect cannot complete a workflow early.
--
-- Only PLAN workflows have a row here; legacy single-operation workflows do not
-- (operation_settle then falls back to the caller-supplied finality, which for a
-- one-step plan is always final).
CREATE TABLE IF NOT EXISTS `tb_mf_workflow_plan` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Stable hash over the ordered (operation, input) steps. Identity of the plan
	-- the workflow was created under; immutable.
	`plan_hash` varbinary(16) NOT NULL,
	-- Number of operations in the plan. The last step is seq == plan_length.
	`plan_length` int NOT NULL,
	`created_at` datetime(6) NOT NULL,
	PRIMARY KEY (`workflow_id`),
	CONSTRAINT `ck_mf_plan_length` CHECK (`plan_length` >= 1),
	CONSTRAINT `ck_mf_plan_hash_len` CHECK (LENGTH(`plan_hash`) = 16),
	-- The plan belongs to its workflow (like operations/checkpoints/events): the
	-- "cannot be orphaned" invariant is STRUCTURAL, not just preserved by the create
	-- procedure.
	CONSTRAINT `fk_mf_plan_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
