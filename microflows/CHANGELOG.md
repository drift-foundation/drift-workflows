# Changelog — microflows / uflowsd

> Pre-1.0 / pre-public-release: **breaking changes are expected** and arrive in minor bumps. The
> participant HTTP surface stays `/microflows/v1/…` — the v1 *contract* tightened, it was not forked to
> v2. Full prose: `work/uflowsd/RELEASE_ANNOUNCEMENT_DRAFT.md`.

## microflows 0.5.0 · uflowsd 0.3.0 — unreleased

Certified driftc 0.33.64 / ABI 18. Root `just test` green: singular, the microflows component (148-check
stored-procedure regression + 20 e2e base/asan/memcheck + 99 parser fixtures), and the coordinator↔singular
integration suite — **208 checks**.

> Released artifacts (versioned in the root `drift/manifest.json`): **singular 0.7.0** (unchanged),
> **microflows 0.5.0**, **uflowsd 0.3.0**. `microflows-runner` (one-shot CLI) + `microflows-participant-stub`
> are **component-local dev artifacts** (version `0.0.0`) — not Drift release artifacts, no release version.

### Contract — GET stays read-only; PUT owns same-operation reclaim/reassert
The participant `/microflows/v1/…` surface keeps a strict read/write split (`microflows_design.md` §5.1.1):

- **`GET {operation_id}` is read-only and NEVER owns reclaim** — it returns terminal | pending | unknown
  only, and never mutates lease/attempt state. A participant that committed its effect but crashed before
  recording the terminal result stays `202 pending` under `GET` indefinitely.
- **`PUT {operation_id}` with the same input is an idempotent reassert/reclaim**, not a fresh execution. A
  **live-working** op ⇒ `202` (the live lease is never stolen); an **expired-working** op ⇒ the PUT MUST
  **reclaim** (with Singular: `resume`), rerun the body idempotently (replaying the committed effect, not
  re-applying it), and complete/replay the recorded result ⇒ `200`.

### Added — case [12]: durable pending→re-dispatch recovery
A *recovered* operation whose participant crashed mid-commit no longer hangs `pending` forever. On a
**confirmed** GET-`202` (not a 5xx/transport blip) of a recovered dispatch, uflowsd escalates a **durable,
fenced** re-dispatch timer and re-issues a byte-identical **PUT**, so the participant reclaims its expired
lease and completes — **exactly once** (the reclaim rerun replays, never re-executes).

- **`DispatchResult::Pending` split** into `PendingObserved` (a confirmed `202` — the **only** thing that
  feeds the timer on a recovered dispatch) vs `PendingUncertain` (transport/5xx/unreachable — plain defer +
  re-poll, never advances the timer). A **fresh** (non-recovered) dispatch never escalates.
- **SPs** `sp_mf_operation_pending_defer` (forward) / `sp_mf_checkpoint_pending_defer` (reverse): fenced +
  atomic, mirroring the #2 reconcile machinery. `defer` (within the interval) releases the lease + sets
  `next_attempt_at` + anchors the epoch + appends the audit event in **one** txn; `redispatch` (interval
  elapsed) advances + re-arms the timer and **keeps** the lease (the runner re-PUTs under it). Unlike the #2
  budget there is **no exhaustion/block** — a re-PUT is idempotent, so it escalates indefinitely (a genuinely
  broken op fails definitively via the rerun's `400` → reversal; a slow-but-alive op keeps answering `202`).
- Wired through one shared helper at **all three** recovered-pending arms — planned forward, legacy single-op
  forward, and reverse compensation.
- **Config:** `deployment.pending_redispatch_after_ms` (int ≥ 0, **default `60000`** ≈ a participant lease
  TTL + margin; SHOULD be set ≥ the deployment's participant lease TTL + margin), validated strictly at
  startup (no silent fallback), like `reconcile_budget`. Migration `db/migrations/0003_pending_redispatch.sql`
  (durable timer columns on the operation row forward / checkpoint row reverse).
- Coverage: SP regression (forward + reverse defer/redispatch boundary, epoch-anchored-once, fence_lost, all
  guardrails, atomic-defer no-partial-state, skew → no-append-still-defers); integration `c12_*` (crash →
  recovered `202` → escalate → reclaim → completed, `exec_count==1`, `redispatch_count` advanced, one
  re-dispatch PUT).

### Migration
Apply `db/migrations/0003_pending_redispatch.sql` (after `0001`/`0002`) — it adds the durable re-dispatch
timer columns (`redispatch_first_seen_at` / `redispatch_last_at` / `redispatch_count`) to `tb_mf_operation`
(forward) and `tb_mf_workflow_checkpoint` (reverse). All NULL/0 by default → existing rows are timer-unused;
online-safe. Fresh installs get these from the schema files directly (no migration needed). Participants:
keep `GET` read-only and ensure a same-`operation_id` `PUT` reclaims/replays an expired-working op (→ `200`)
rather than reporting `202` forever — the coordinator now drives recovery via that re-PUT.

## microflows 0.4.0 · uflowsd 0.2.0 — superseded by 0.5.0 (never cut)

Certified driftc 0.33.63 / ABI 18. Root `just test` green (singular, microflows component, and the
coordinator↔singular integration suite — 202 checks).

> Released artifacts (versioned in the root `drift/manifest.json`): **singular 0.7.0**, **microflows 0.4.0**,
> **uflowsd 0.2.0**. `microflows-runner` (one-shot CLI) + `microflows-participant-stub` are **component-local
> dev artifacts** (version `0.0.0`) — not Drift release artifacts, no release version.

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
