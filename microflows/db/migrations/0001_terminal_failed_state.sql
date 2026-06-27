-- Migration 0001 — terminal STATE_FAILED(7) + durable terminal_reason
-- ============================================================================
-- Adds the `failed`(7) lifecycle state and the `terminal_reason` column, and
-- BACKFILLS legacy `reversed`(5) rows deterministically so client REPLAY renders
-- the same outcome from durable state.
--
-- Step 4 of the failed/compensated work (see microflows_design.md / the runner's
-- Outcome::TerminalFailure). Apply ONCE to an existing `microflows` schema; fresh
-- installs get this from schema/tb_mf_workflow.sql directly (no migration needed).
--
-- Background: before this change, an empty-stack reversal (a definite forward
-- rejection with NO compensable checkpoints) and a real completed unwind BOTH
-- terminated at state=5. The client now distinguishes them:
--   state 5 -> {"workflow":"failed","reason":...,"compensated":true}   (unwind ran)
--   state 7 -> {"workflow":"failed","reason":...,"compensated":false}  (nothing unwound)
--
-- The legacy audit already separates them:
--   * old EMPTY path wrote a kind='reversed' event with payload.compensated = 0
--     and NO compensation_settled events.
--   * a real unwind wrote kind='reversal_begun' + one-or-more
--     kind='compensation_settled' events.
-- ============================================================================

-- 1. Schema: column + state CHECKs (drop+add; MariaDB has no ALTER-CHECK-in-place).
ALTER TABLE `tb_mf_workflow`
	ADD COLUMN `terminal_reason` varchar(190) NULL AFTER `reversal_trigger_operation_id`;

ALTER TABLE `tb_mf_workflow` DROP CONSTRAINT `ck_mf_workflow_state`;
ALTER TABLE `tb_mf_workflow` ADD  CONSTRAINT `ck_mf_workflow_state`
	CHECK (`state` IN (1,2,3,4,5,6,7));

ALTER TABLE `tb_mf_workflow` DROP CONSTRAINT `ck_mf_workflow_state_disposition`;
ALTER TABLE `tb_mf_workflow` ADD  CONSTRAINT `ck_mf_workflow_state_disposition`
	CHECK ((`state` = 1 AND `current_disposition` = 0) OR (`state` = 2 AND `current_disposition` IN (2,3)) OR (`state` = 3 AND `current_disposition` IN (2,3,4)) OR (`state` = 4 AND `current_disposition` = 1) OR (`state` = 5 AND `current_disposition` IN (2,3)) OR (`state` = 6 AND `current_disposition` IN (2,3,4)) OR (`state` = 7 AND `current_disposition` IN (2,3)));

ALTER TABLE `tb_mf_workflow` DROP CONSTRAINT `ck_mf_workflow_state_direction`;
ALTER TABLE `tb_mf_workflow` ADD  CONSTRAINT `ck_mf_workflow_state_direction`
	CHECK ((`state` = 1 AND `execution_direction` = 1) OR (`state` = 2 AND `execution_direction` = 2) OR (`state` = 3 AND `execution_direction` IN (1,2)) OR (`state` = 4 AND `execution_direction` = 1) OR (`state` = 5 AND `execution_direction` = 2) OR (`state` = 6 AND `execution_direction` IN (1,2)) OR (`state` = 7 AND `execution_direction` = 2));

-- 2a. Real completed unwinds: state=5 WITH a compensation_settled event -> keep
--     reversed(5); backfill terminal_reason from the reversal_begun audit.
UPDATE `tb_mf_workflow` w
SET w.`terminal_reason` = COALESCE(
		(SELECT JSON_UNQUOTE(JSON_EXTRACT(e.`payload`, '$.reason'))
		 FROM `tb_mf_workflow_event` e
		 WHERE e.`workflow_id` = w.`workflow_id` AND e.`kind` = 'reversal_begun'
		 ORDER BY e.`event_seq` DESC LIMIT 1),
		'legacy_reversal')   -- FALLBACK (documented): no reversal_begun reason found.
WHERE w.`state` = 5
  AND EXISTS (SELECT 1 FROM `tb_mf_workflow_event` e2
              WHERE e2.`workflow_id` = w.`workflow_id` AND e2.`kind` = 'compensation_settled');

-- 2b. Legacy EMPTY reversals: state=5 with NO compensation_settled event (the old
--     direct forward->reversed shortcut) -> migrate to failed(7); reason from the
--     old kind='reversed' event payload.
UPDATE `tb_mf_workflow` w
SET w.`state` = 7,
    w.`terminal_reason` = COALESCE(
		(SELECT JSON_UNQUOTE(JSON_EXTRACT(e.`payload`, '$.reason'))
		 FROM `tb_mf_workflow_event` e
		 WHERE e.`workflow_id` = w.`workflow_id` AND e.`kind` = 'reversed'
		 ORDER BY e.`event_seq` DESC LIMIT 1),
		'legacy_empty_reversal')   -- FALLBACK (documented): no 'reversed' event reason found.
WHERE w.`state` = 5
  AND NOT EXISTS (SELECT 1 FROM `tb_mf_workflow_event` e2
                  WHERE e2.`workflow_id` = w.`workflow_id` AND e2.`kind` = 'compensation_settled');

-- 2c. Any remaining state=5 left by 2a (terminal_reason still NULL is impossible
--     after 2a's COALESCE) is intentionally kept as reversed(5)=unwind-completed;
--     a row that matched NEITHER 2a NOR 2b cannot exist (every legacy reversed got
--     there via exactly one of the two paths). If operational reality proves
--     otherwise, such a row keeps state=5 with a NULL terminal_reason and the
--     renderer falls back to an empty reason — investigate, do not silently 7-flip.
