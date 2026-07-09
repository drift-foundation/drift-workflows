DELIMITER $$
-- READ-ONLY operability inspection (v1 ScriptRegistry): list the workflows currently
-- STALLED on an unavailable pinned plan — nonterminal, lease cleared, recoverable, and
-- whose latest durable condition is a `revision_unavailable` dispatch deferral.
--
-- A worker that cannot satisfy a workflow's pinned plan (version/content_hash not in
-- its loaded generation) durably defers with reason 'revision_unavailable'
-- (sp_mf_operation_dispatch_defer): state/direction/continuation unchanged, lease
-- cleared, a single deduplicated 'operation_dispatch_deferred' audit event on entry.
-- The workflow stays forward(1)/reversing(2) and claimable, so restoring a compatible
-- plan generation lets it continue automatically. This proc surfaces exactly that
-- population for an operator — NOT a participant failure, NOT blocked_resolution.
--
-- "Latest condition" = the event at w.current_event_ts (the durable latest-event
-- projection). We report a row only when THAT event is the revision_unavailable
-- deferral, so a workflow that has since advanced (a newer event) drops off.
--
-- Exposes, per stalled workflow: id, pinned (script_name, plan_version, content_hash,
-- plan_length), workflow state + execution_direction, the current/last event timestamp,
-- the next scheduled attempt, and the durable reason. Returns a JSON ARRAY (possibly
-- empty). Read-only: no lease, no fence, no mutation.
CREATE PROCEDURE `sp_mf_plan_stalled`()
proc:BEGIN
	SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
		'workflow_id', LOWER(HEX(`w`.`workflow_id`)),
		'script_name', `w`.`script_name`,
		'plan_version', `p`.`plan_version`,
		'content_hash', LOWER(HEX(`p`.`content_hash`)),
		'plan_length', CAST(`p`.`plan_length` AS SIGNED),
		'state', CAST(`w`.`state` AS SIGNED),
		'execution_direction', CAST(`w`.`execution_direction` AS SIGNED),
		'current_event_ts', DATE_FORMAT(`w`.`current_event_ts`, '%Y-%m-%dT%H:%i:%s.%fZ'),
		'next_attempt_at', DATE_FORMAT(`w`.`next_attempt_at`, '%Y-%m-%dT%H:%i:%s.%fZ'),
		'reason', JSON_UNQUOTE(JSON_EXTRACT(`e`.`payload`, '$.reason'))
	)), JSON_ARRAY()) AS result
	FROM `tb_mf_workflow` `w`
	JOIN `tb_mf_workflow_plan` `p` ON `p`.`workflow_id` = `w`.`workflow_id`
	JOIN `tb_mf_workflow_event` `e`
		ON `e`.`workflow_id` = `w`.`workflow_id` AND `e`.`event_ts` = `w`.`current_event_ts`
	WHERE `w`.`state` IN (1, 2)
	  -- Genuinely STALLED = the deferral's cleared lease is still cleared. A workflow that
	  -- has since been reclaimed (lease_owner set) is actively retrying, not stalled, even
	  -- if its latest event is still the revision_unavailable deferral — exclude it.
	  AND `w`.`lease_owner` IS NULL
	  AND `e`.`kind` = 'operation_dispatch_deferred'
	  AND JSON_UNQUOTE(JSON_EXTRACT(`e`.`payload`, '$.reason')) = 'revision_unavailable';
END $$
DELIMITER ;
