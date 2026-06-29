# Workflow Composition — Progress

## Status

Release is cut + announced. Design at `DESIGN.md` is at **rev 2** (K review rounds folded in). **Decision #1
(transport) RESOLVED → internal durable host API, async + awaited** — a workflow call is a pending **call
operation**; the parent stays in normal forward execution, and a blocked child does **not** cascade up the
call tree. **Decision #2 (slice plan) RESOLVED** — slice 1 ships the async call spine **without T1**.
Terminology fixed (workflow call / child workflow / call operation; avoid "callback").

**Slice plan (decided):**
- **Slice 1** — async workflow-call spine, **no T1**: no-comp / **compensating-workflow** compensation
  (fresh comp child, no `completed→reversing` reopen). Boundary: workflow calls as async awaited operations —
  **no inline child execution, no blocked cascade, no reverse-child reopen.**
- **Slice 2** — **reverse-child + T1** `completed(4)→reversing(2)`, plus the **stuck-child liveness budget**.
- **Slice 3** — **fan-out** + `on failed`-as-data / typed failure union.

The slice-1 **build checklist** (grammar → IR/validation → schema/SP/host → runner → docs/tests) is in
`DESIGN.md` and is concrete enough to start implementation from.

**Design-review pass folded in (5 findings):** (1) the runtime **recursion guard** now keys on the
**ancestor SET of plan-identity keys** `(script_name, plan_version, content_hash)` — child ids are freshly
derived per call, so an instance-id ancestor check can't catch an A→B→A cycle; plus `max_call_depth`. The key
needs **no new workflow column** (`content_hash`/`plan_version` are already in `tb_mf_workflow_plan`).
(2) The **call-operation storage model** is now concrete: `operation_id = child_workflow_id`;
`schema_version`/`status`/`result_json` keep their existing meaning + invariant (NOT overloaded with the
child plan revision); child plan identity lives in call-specific columns `child_script_name` /
`child_plan_version` / `child_content_hash`; `sp_mf_call_submit` is a **sibling** of
`sp_mf_operation_request` (not a caller). (3) `child_terminal_notify` is **wake/stage-only** — the runner +
`call_inspect` is the single authoritative settle/reversal. (4)/(5) Stale open-decision sections retired and
the durable-state sketch marked slice-neutral (liveness columns = slice 2).

**Second review round folded in (storage/identity hardening):** (1) **`call <child>@<plan_version>`** is now
explicit — `@` is a **semantic plan version** (`major.minor.patch`), registry-resolved by exact-match to the
pinned `content_hash` (the same plan-pin model as a top-level workflow); not an opaque alias, not a
participant `schema_version`. (2) The call op's **`operation_name = child_script_name`** (no prefix → no `varchar(128)`
overflow; **`call_kind`** is the discriminator; `sp_mf_operation_settle` copies it into the checkpoint, so
it is the compensation envelope's `forward.operation`). The call op's `schema_version` is a named constant
**`CALL_OPERATION_SCHEMA_VERSION = 1`**, and the comp **`{forward:{…}}`** envelope carries the full
correlation set (`workflow_id`, `operation`, **`operation_id = child_workflow_id`**, `schema_version`,
`input`, `result`). Liveness is **split**: slice 1 ships terminal push + poll fallback; slice 2 decides only
the stuck-child budget. (3) The compensation workflow is pinned by its **exact plan identity** (`comp_script_name`
/ `comp_plan_version` / `comp_content_hash`), mirroring the checkpoint's `reverse_operation_name`+
`reverse_schema_version` pin — not a loose `name@rev`. (4) The recursion ancestor set is **reconstructed by
walking `parent_workflow_id` links + joining `tb_mf_workflow_plan`** (bounded by `call_depth`) — no
denormalized ancestor column. (5) Doc title aligned with this index.

## Current Scope

Design complete (`DESIGN.md` rev 2); **slice 1 is the active build scope.** Overall feature:

- any workflow step may be a child workflow;
- child workflow owns its own durable state;
- parent treats the child call as one forward step/checkpoint with result data flow;
- compensations may be workflows;
- dynamic fan-out uses stable child workflow ids, not operation occurrence indexes.

## Verification

None yet. No code changes are part of this effort.

## Dirty Worktree

The three work files (`README.md`, `PROGRESS.md`, `DESIGN.md`) are committed; the current dirty state is only
`DESIGN.md` + `PROGRESS.md` (the review-pass design refinements). No code changes.

## Literal Next Action

Design is settled enough to build. **Start slice 1 from the `DESIGN.md` build checklist, in dependency
order:** (1) grammar (`call <child>@<plan_version> { … }` + optional `compensation`), (2) IR `NCallWorkflow` +
contract/cycle validation, (3) schema `0003_workflow_call.sql` + SPs (`call_submit`/`call_inspect`/
`child_terminal_notify`/`comp_submit`) + host wrappers, (4) runner dispatch/await/reverse, (5) docs +
end-to-end acceptance (completed / failed / blocked-no-cascade / recovery / recursion-guard /
compensating-workflow) with root `just test` green. Each stage lands with its own tests.
