-- (File named tb_mf_workflow_operation_call.sql so Mariachi applies it AFTER both parent tables
--  tb_mf_workflow.sql and tb_mf_workflow_operation.sql; Mariachi orders schema files by filename,
--  FK checks on. The table itself is tb_mf_call.)
-- Composition (1b.1) — durable sidecar for a workflow-to-workflow call, 1:1 with the
-- tb_mf_operation row that represents it (call_kind=2, work/workflow-composition/DESIGN.md
-- §Durable state). All call-specific state lives HERE, not on the generic, hot tb_mf_operation
-- table — deliberately, so that table is not widened with ~9 mostly-NULL columns.
--
-- child_status is a DISPLAY HINT ONLY (§Liveness) — the child workflow row (tb_mf_workflow, by
-- child_workflow_id) is authoritative; sp_mf_call_inspect re-reads it directly and NEVER trusts
-- this column. Refreshed by sp_mf_child_terminal_notify (the terminal case: wake + hint) and the
-- separate best-effort sp_mf_call_hint_refresh (the non-terminal case, in particular `blocked`) —
-- never by sp_mf_call_inspect itself, which is a pure read.
--
-- No `child_return_json` value-of-record (notify never stages the child's return; settle re-reads
-- child truth via call_inspect) and no liveness/stuck-child budget columns (slice 2, standalone).
-- No compensation-plan-identity columns either: 1c's single MVP mechanism (T1, reverse-child
-- reopen) compensates a completed child by reopening the CHILD's OWN workflow row
-- (`completed(4)->reversing(2)`) via child_workflow_id already below — it never pins a separate
-- compensation-workflow identity (that model, compensating-workflow, is explicitly out of MVP).
--
-- child_status codes: 1=pending 2=completed 3=failed 4=blocked
CREATE TABLE IF NOT EXISTS `tb_mf_call` (
	`workflow_id` varbinary(16) NOT NULL,
	`operation_seq` int NOT NULL,
	-- Also the op row's operation_id (denormalized here so the sidecar is self-contained).
	`child_workflow_id` varbinary(16) NOT NULL,
	-- Child plan identity — the SAME plan-pin model as any top-level workflow (exact-match
	-- resolution against tb_mf_workflow_plan, §22). Used by the recursion guard's ancestor walk.
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
	-- The child is created in the SAME transaction, before this row (sp_mf_call_submit's write
	-- phase) — so "cannot point at a missing child" is structural here, matching
	-- tb_mf_workflow_plan's / tb_mf_workflow_args' own FK-to-tb_mf_workflow pattern.
	CONSTRAINT `fk_mf_call_child` FOREIGN KEY (`child_workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
