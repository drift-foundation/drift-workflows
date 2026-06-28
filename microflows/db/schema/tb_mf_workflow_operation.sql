-- (File named tb_mf_workflow_operation.sql so Mariachi applies it AFTER the
--  parent tb_mf_workflow.sql; Mariachi orders schema files by filename, FK
--  checks on. The table itself is tb_mf_operation.)
-- Remote operation request + result — one row per operation invocation in a
-- workflow (microflows_design.md §2.5, §5; first-slice operation store).
--
-- A Microflows operation is a typed remote call to a participant. Its request
-- (input + the stable operation id used as the participant key) is persisted
-- ATOMICALLY with the suspended continuation BEFORE dispatch (§2.5): after a
-- crash, recovery finds the request and reconciles by operation_id; it never
-- loses track of an operation it may have sent. The result is recorded only
-- after a durable success, together with the Checkpoint (sp_mf_operation_settle).
--
-- status codes:
--   1 = requested  (persisted before dispatch; outcome not yet durably known)
--   2 = succeeded  (a durable success result recorded; Checkpoint created)
--
-- operation_id is the stable, caller(Microflows)-derived participant key,
-- unique within a workflow run. Time discipline (§24.4): explicit timestamps,
-- no NOW(); event ordering is event_seq.
CREATE TABLE IF NOT EXISTS `tb_mf_operation` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Static call-site position of this operation within the workflow.
	`operation_seq` int NOT NULL,
	-- Stable operation id (the participant key): derived from workflow instance
	-- + pinned revision + call site + invocation; collision-resistant.
	`operation_id` varbinary(16) NOT NULL,
	`operation_name` varchar(128) NOT NULL,
	-- Pinned input/output contract revision for this operation (design §2.5).
	-- Persisted with the request so recovery dispatches against the SAME contract
	-- the workflow was suspended on, never a renegotiated one.
	`schema_version` int NOT NULL,
	-- The typed input document (JSON OBJECT) and its canonical hash.
	`input_json` mediumtext NOT NULL CHECK (json_valid(`input_json`) AND json_type(`input_json`) = 'OBJECT'),
	`input_hash` varchar(64) NOT NULL,
	`status` tinyint NOT NULL,
	-- The durable success result (JSON OBJECT), set on settle; NULL while requested.
	`result_json` mediumtext NULL,
	`created_at` datetime(6) NOT NULL,
	`updated_at` datetime(6) NOT NULL,
	-- Durable bounded reconcile budget for persistent participant route-404s (#2). Advanced ONLY by
	-- sp_mf_workflow_reconcile_defer on a confirmed Route404. Keyed by (workflow_id, operation_seq) so a
	-- resume re-reads the same row and the budget never resets. NULL/0 until the first route-404.
	`reconcile_attempts` int NOT NULL DEFAULT 0,
	`reconcile_first_seen_at` datetime(6) NULL,
	`reconcile_last_seen_at` datetime(6) NULL,
	`reconcile_reason` varchar(64) NULL,
	PRIMARY KEY (`workflow_id`,`operation_seq`),
	UNIQUE KEY `uq_mf_operation_opid` (`operation_id`),
	CONSTRAINT `ck_mf_operation_status` CHECK (`status` IN (1,2)),
	-- status/result invariant: requested has no result; succeeded has a valid
	-- JSON-object result. (requested + result, or succeeded + NULL, are
	-- unrepresentable.)
	CONSTRAINT `ck_mf_operation_status_result` CHECK ((`status` = 1 AND `result_json` IS NULL) OR (`status` = 2 AND `result_json` IS NOT NULL AND json_valid(`result_json`) AND json_type(`result_json`) = 'OBJECT')),
	CONSTRAINT `fk_mf_operation_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
