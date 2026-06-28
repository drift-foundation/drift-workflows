# Changelog — microflows / uflowsd

> Pre-1.0 / pre-public-release: **breaking changes are expected** and arrive in minor bumps. The
> participant HTTP surface stays `/microflows/v1/…` — the v1 *contract* tightened, it was not forked to
> v2. Full prose: `work/uflowsd/RELEASE_ANNOUNCEMENT_DRAFT.md`.

## microflows 0.4.0 · uflowsd / microflows-runner 0.2.0 — unreleased

Certified driftc 0.33.63 / ABI 18. Root `just test` green (singular, microflows component, and the
coordinator↔singular integration suite — 202 checks).

### ⚠️ BREAKING (see RELEASE_ANNOUNCEMENT_DRAFT.md → *Breaking changes* + *Migration*)
- **Participant `200` contract is result-only.** A `200` body must be `{"result":{…}}` with `result` an
  **object**; `state` is not read on a 200 (advisory). Missing / non-object `result` is a definite
  protocol failure (`participant_protocol_missing_result` / `…_invalid_result`) → workflow `failed`.
  A business-negative outcome is a `200` **result** the workflow branches on, never `200 {state:"failed"}`.
- **Client outcome vocabulary.** `{"workflow":"reversed"}` (exit 0) is **removed**; consumers read
  `{"workflow":"failed","reason","compensated"}` (exit 3). Read the outcome *document*, not the HTTP/exit.
- **Compensation request body** is the `{"forward":{input,result,…}}` envelope (was the bare forward input).
- **`.mf` source**: `#` comments are gone — C-family only (`//`, `/* */`).

### Added
- **#2 — durable bounded reconcile budget for persistent participant `404`s.** A confirmed route-404
  advances a fenced, durable budget (on the operation row forward / checkpoint row reverse); within budget
  it defers + retries, on exhaustion (wall-time **and** a min-attempts floor) it enters **`blocked`**
  (forward: indeterminate; reverse: checkpoint `resolution_required`) — never a silent infinite pending.
  Config: `deployment.reconcile_budget.{max_elapsed_ms, min_attempts}` (default 30 min / 2), validated
  strictly at startup. Migration `db/migrations/0002_reconcile_budget.sql`.
- **#5 — node-address operation ids** `H(workflow_id, content_hash, node_id)`; resume adopts the durable id.
- **Result-conditional branching + authored `fail`** (`if`/`case` path selectors; `fail "<reason>"`).
- **`failed` / `compensated` durable terminal** (state 7; migration `0001_terminal_failed_state.sql`).
- **`runner --emit-graph`** — lower a `.mf` to a Mermaid `flowchart` (DB-free); used by `microflows-viz`.

### Migration
Apply `db/migrations/0001_terminal_failed_state.sql` then `0002_reconcile_budget.sql`. Participants: ensure
every `200` carries an object `result`; move terminal-failure signaling out of `state` into a `result`;
update compensation handlers to the `{"forward":{…}}` envelope. Workflow authors: convert `#` comments to `//`.
