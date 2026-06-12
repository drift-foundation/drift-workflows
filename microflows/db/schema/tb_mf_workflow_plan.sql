-- Durable PIN of a workflow's manual-IR script revision (§10/§22, milestone-1).
--
-- A workflow pins, at creation, the IMMUTABLE identity of the plan it runs:
-- `(script_name, plan_version, content_hash, plan_length)`. `script_name` lives on
-- tb_mf_workflow; the plan's immutable SEMANTIC VERSION (major.minor.patch) + its
-- `content_hash` + `plan_length` live here. The ScriptRegistry resolves the IR by
-- (name, plan_version) and the runner verifies the resolved plan's content_hash +
-- length against this pin — v1 selection is EXACT-MATCH on (version AND content_hash),
-- and the runner NEVER substitutes a different plan. An existing version must never be
-- republished with different content; a plan the loaded generation cannot satisfy is a
-- revision_unavailable defer (recoverable), never a substitution.
--
-- major.minor.patch is the future COMPATIBILITY vocabulary (major=boundary,
-- minor=backward-compatible capability, patch=compatible correction); v1 does not yet
-- use ranges — selection is exact-match. (script_revision on tb_mf_workflow remains a
-- legacy int, unused by the plan model.)
--
-- `content_hash` is a collision-resistant, VERSIONED digest: a 1-byte scheme tag
-- followed by a 32-byte SHA-256 over the canonical revision IR = 33 bytes fixed.
-- (Distinct from the per-operation input_hash, which keys participant idempotency.)
--
-- `plan_length` makes operation finality DURABLE: operation_request/settle and
-- terminal replay read it from storage, never from the registry. The last step is
-- seq == plan_length.
--
-- Only PLAN workflows have a row here; legacy single-operation workflows do not.
CREATE TABLE IF NOT EXISTS `tb_mf_workflow_plan` (
	`workflow_id` varbinary(16) NOT NULL,
	-- Immutable plan version: validated semantic version major.minor.patch. The pin
	-- is `(script_name, plan_version, content_hash, plan_length)`; resolution is
	-- exact-match on this version (ranges are a future, not-yet-built, evolution).
	`plan_version` varchar(32) NOT NULL,
	-- Versioned content digest of the pinned plan: 0x01 scheme byte ‖
	-- SHA-256(canonical IR) = 33 bytes. Immutable identity ("never substitute").
	`content_hash` varbinary(33) NOT NULL,
	-- Number of operations in the plan. The last step is seq == plan_length.
	`plan_length` int NOT NULL,
	`created_at` datetime(6) NOT NULL,
	PRIMARY KEY (`workflow_id`),
	CONSTRAINT `ck_mf_plan_length` CHECK (`plan_length` >= 1),
	CONSTRAINT `ck_mf_content_hash_len` CHECK (LENGTH(`content_hash`) = 33),
	-- Semantic version shape (major.minor.patch, non-negative integers). The full
	-- immutability/exact-match contract is enforced by the runner + create proc; this
	-- guards the stored shape.
	CONSTRAINT `ck_mf_plan_version` CHECK (`plan_version` REGEXP '^[0-9]+\\.[0-9]+\\.[0-9]+$'),
	-- The plan belongs to its workflow (like operations/checkpoints/events): the
	-- "cannot be orphaned" invariant is STRUCTURAL, not just preserved by the create
	-- procedure.
	CONSTRAINT `fk_mf_plan_workflow` FOREIGN KEY (`workflow_id`) REFERENCES `tb_mf_workflow` (`workflow_id`)
) ENGINE=InnoDB;
