-- Durable per-instance workflow ARGUMENTS (manual-IR frontend, Step 1).
--
-- Each workflow instance receives exactly ONE JSON object: its arguments. The script
-- declares a closed-field argument TYPE (that type is part of the script content_hash —
-- changing the contract is a new revision); only these per-instance argument VALUES live
-- here, and they are NOT part of content_hash.
--
-- `args_canonical` is the arguments rendered as ORDERED-KEY COMPACT CANONICAL JSON, stored
-- as UTF-8 BYTES (a binary column). Storing the canonical form lets a workflow_id reuse be
-- judged by a BYTE-FOR-BYTE comparison — never collation-sensitive SQL text equality, and
-- never a caller-supplied hash. Immutable once written.
--
-- Written ONLY by sp_mf_workflow_create_planned, in the SAME transaction as the workflow +
-- plan-pin rows, so the arguments can never be orphaned and the create command is their sole
-- author (one single-`workflow_id` aggregate commit; see doc/storage_portability.md). Resume
-- reads this durable record (sp_mf_args_get), never submission/CLI input.
CREATE TABLE IF NOT EXISTS `tb_mf_workflow_args` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Canonical arguments document: ordered-key compact JSON, UTF-8 bytes. Compared
	-- byte-for-byte (binary column → no collation). Validated as a JSON object by the
	-- create procedure before insert.
	`args_canonical` mediumblob NOT NULL,
	`created_at` datetime(6) NOT NULL,
	PRIMARY KEY (`workflow_id`),
	-- At minimum the empty object `{}` (2 bytes); fuller JSON-object validity is enforced
	-- by the create procedure (a binary column cannot carry a JSON CHECK directly).
	CONSTRAINT `ck_mf_args_nonempty` CHECK (LENGTH(`args_canonical`) >= 2),
	-- Arguments belong to their workflow (like the plan/operations/checkpoints/events): the
	-- "cannot be orphaned" invariant is STRUCTURAL, not just preserved by the procedure.
	CONSTRAINT `fk_mf_args_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
