# Workflow Composition — Progress

## Status

Release is cut + announced. Design at `DESIGN.md` is at **rev 2** (K rounds 1–3 folded in). **Decision #1
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

This effort adds `work/workflow-composition/README.md`, `PROGRESS.md`, and `DESIGN.md` (the revised first design pass). No code changes.

## Literal Next Action

Design is settled enough to build. **Start slice 1 from the `DESIGN.md` build checklist, in dependency
order:** (1) grammar (`call <child>@<rev> { … }` + optional `compensation`), (2) IR `NCallWorkflow` +
contract/cycle validation, (3) schema `0003_workflow_call.sql` + SPs (`call_submit`/`call_inspect`/
`child_terminal_notify`/`comp_submit`) + host wrappers, (4) runner dispatch/await/reverse, (5) docs +
end-to-end acceptance (completed / failed / blocked-no-cascade / recovery / recursion-guard /
compensating-workflow) with root `just test` green. Each stage lands with its own tests.
