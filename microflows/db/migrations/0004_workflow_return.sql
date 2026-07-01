-- Migration 0004 — durable workflow TERMINAL RETURN (1b.0a step 3)
-- ============================================================================
-- Adds `workflow_return_json` to `tb_mf_workflow`: the AUTHORITATIVE typed
-- workflow return, separate from any per-operation result (tb_mf_operation.
-- result_json). Written ATOMICALLY with completion by sp_mf_operation_settle's
-- final-settle branch (the SAME UPDATE that flips state->completed) — never a
-- second write. Terminal replay (sp_mf_workflow_inspect) reads this column
-- directly, never re-derives from the graph.
--
-- Apply ONCE to an existing `microflows` schema; fresh installs get this from
-- schema/tb_mf_workflow.sql directly (no migration needed).
--
-- Backfill rationale: before this feature, `return` was parse-gated and every
-- graph's implicit terminal was unit (external `{}`) — there was no way to
-- produce a non-unit terminal. So every PRE-EXISTING completed(4) row is, by
-- construction, a unit return: backfilling `{}` is not a guess, it is the only
-- value consistent with what could have happened under the prior engine.
-- Non-completed / failure-terminal rows are left NULL (mirrors how
-- `terminal_reason`, added by migration 0001, is NULL on non-failure rows).
-- ============================================================================

-- 1. Schema: add the column (nullable, no CHECK yet — MariaDB has no in-place
--    ALTER-CHECK, so the constraint is added in step 3, after backfill).
ALTER TABLE `tb_mf_workflow`
	ADD COLUMN `workflow_return_json` mediumtext NULL AFTER `terminal_reason`;

-- 2. Backfill: every existing completed(4) row is a unit return.
UPDATE `tb_mf_workflow`
SET `workflow_return_json` = '{}'
WHERE `state` = 4;

-- 3. Constraint: completed(4) <=> a valid JSON-object return is present; every
--    other state <=> NULL. Mirrors the (state, disposition)/(state, direction)
--    checks already on this table, and ck_mf_operation_status_result's
--    bidirectional shape on tb_mf_operation.
ALTER TABLE `tb_mf_workflow` ADD CONSTRAINT `ck_mf_workflow_state_return`
	CHECK (
		(`state` = 4 AND `workflow_return_json` IS NOT NULL
			AND JSON_VALID(`workflow_return_json`) AND JSON_TYPE(`workflow_return_json`) = 'OBJECT')
		OR (`state` <> 4 AND `workflow_return_json` IS NULL)
	);
