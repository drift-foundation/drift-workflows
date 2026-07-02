# Release — microflows 0.5.0 · uflowsd 0.3.0 (singular 0.7.0)

**Status: CERTIFIED.** Certified driftc 0.33.64 / ABI 18. Root `just test` green end-to-end:
singular, the microflows component (148-check stored-procedure regression + 20 e2e base/asan/memcheck +
99 parser fixtures), and the coordinator↔singular integration suite — **208 checks**.

> Pre-1.0 / pre-public-release: **breaking changes are expected** and arrive in minor bumps. The participant
> HTTP surface stays `/microflows/v1/…` — the v1 *contract* tightened, it was not forked to v2.
>
> 0.5.0 supersedes the never-cut 0.4.0: this is the **first cut of the whole bundle** — the 0.4.0 work
> (result-only contract, #2 reconcile budget, #5 node-address ids, result-conditional branching/`fail`, the
> `failed`/`compensated` terminal) **plus** the new case-[12] pending→re-dispatch recovery.

| Artifact | Version | Change |
|---|---|---|
| `singular` (package) | **0.7.0** | unchanged this cut (PR2 expired-lease reclaim via `resume`) |
| `microflows` (package, code + `microflows/db`) | **0.5.0** | ↑ from 0.4.0 |
| `uflowsd` (app / coordinator daemon) | **0.3.0** | ↑ from 0.2.0 (dep microflows 0.5) |

`mfrunner` (one-shot CLI) and `microflows-participant-stub` remain component-local dev artifacts
(`0.0.0`) — not Drift release artifacts.

---

## Headline — the participant contract: GET stays read-only, PUT owns same-operation reclaim/reassert

The `/microflows/v1/…` surface enforces a strict read/write split (`microflows_design.md` §5.1.1):

- **`GET {operation_id}` is read-only and NEVER owns reclaim.** It returns **terminal | pending | unknown**
  only and never mutates lease or attempt state. A participant that committed its side effect but crashed
  before recording the terminal result stays `202 pending` under `GET` — indefinitely.
- **`PUT {operation_id}` with the same input is an idempotent reassert/reclaim**, not a fresh execution:
  - a **live-working** op ⇒ `PUT` returns **`202`** and the live lease is **never stolen**;
  - an **expired-working** op ⇒ `PUT` **MUST reclaim** (with Singular: `resume`), rerun the body
    idempotently (replaying the committed effect, not re-applying it), and complete/replay the recorded
    result ⇒ **`200`**.
- A `200` is **result-only**: `{"result":{…}}` with `result` an **object**; `state` is advisory on a 200.

Because `GET` can't make progress on a committed-but-uncompleted op, **recovery is coordinator-driven via
PUT** — the coordinator re-PUTs, it does not poll forever.

## Recovery — two durable, fenced safety nets

**case [12] — pending → re-dispatch (NEW in 0.5.0).** A recovered operation whose participant crashed
mid-commit no longer hangs `pending` forever. On a **confirmed** GET-`202` (not a 5xx/transport blip) of a
recovered dispatch, uflowsd escalates a **durable, fenced** re-dispatch timer and re-issues a byte-identical
**PUT**, so the participant reclaims its expired lease and completes — **exactly once** (the reclaim rerun
replays, never re-executes).
- `DispatchResult` split: `PendingObserved` (a confirmed 202 — the only thing that feeds the timer on
  recovery) vs `PendingUncertain` (transport/5xx/unreachable — plain defer, never advances the timer). A
  fresh dispatch never escalates.
- Atomic SPs `sp_mf_operation_pending_defer` / `sp_mf_checkpoint_pending_defer`: `defer` (within the
  interval) releases the lease + sets `next_attempt_at` + anchors the epoch + appends the audit event in one
  txn; `redispatch` (interval elapsed) advances + re-arms the timer and **keeps** the lease.
- **No exhaustion/block** — a re-PUT is idempotent, so it escalates indefinitely (a genuinely broken op fails
  definitively via the rerun's `400` → reversal; a slow-but-alive op keeps answering `202`).
- One shared helper at **all three** recovered-pending arms — planned forward, legacy single-op forward, and
  reverse compensation.
- Config: `deployment.pending_redispatch_after_ms` (int ≥ 0, **default `60000`** ≈ a participant lease TTL +
  margin; SHOULD be ≥ the deployment's participant lease TTL + margin), strictly validated at startup.

**#2 — durable bounded reconcile budget for persistent `404`s (ships in this cut).** A *confirmed* route-404
(a re-PUT 404, or a GET-after-resubmit 404 — never a 202/5xx/transport blip) advances a fenced, durable
budget; within budget the workflow defers + retries, on exhaustion (wall-time **and** a min-attempts floor)
it enters **`blocked`** (forward: indeterminate; reverse: checkpoint `resolution_required`) — never a silent
infinite pending. Config: `deployment.reconcile_budget.{max_elapsed_ms, min_attempts}` (default 30 min / 2).

Both budgets live on the durable operation row (forward) / checkpoint row (reverse), keyed so a resume can
**never** reset their epoch.

## Also in this cut

- **#5 — node-address operation ids** `H(workflow_id, content_hash, node_id)`; resume adopts the durable id.
- **Result-conditional branching + authored `fail`** (`if`/`case` path selectors; `fail "<reason>"`).
- **`failed` / `compensated` durable terminal** (workflow state 7).
- **`runner --emit-graph`** — lower a `.mf` to a Mermaid `flowchart` (DB-free); used by `microflows-viz`.

## ⚠️ Breaking changes

- **Participant `200` is result-only.** A `200` body must be `{"result":{…}}` with `result` an **object**;
  `state` is not read on a 200. Missing / non-object `result` is a definite protocol failure
  (`participant_protocol_missing_result` / `…_invalid_result`) → workflow `failed`. A business-negative
  outcome is a `200` **result** the workflow branches on, never `200 {state:"failed"}`.
- **Client outcome vocabulary.** `{"workflow":"reversed"}` (exit 0) is **removed**; consumers read
  `{"workflow":"failed","reason","compensated"}` (exit 3). Read the outcome *document*, not the HTTP/exit.
- **Compensation request body** is the `{"forward":{input,result,…}}` envelope (was the bare forward input).
- **`.mf` source**: `#` comments are gone — C-family only (`//`, `/* */`).

## Migration

Apply the DB migrations in order: `0001_terminal_failed_state.sql` → `0002_reconcile_budget.sql` →
`0003_pending_redispatch.sql`. `0003` adds the re-dispatch timer columns (`redispatch_first_seen_at` /
`redispatch_last_at` / `redispatch_count`) to `tb_mf_operation` (forward) and `tb_mf_workflow_checkpoint`
(reverse) — all NULL/0 by default → existing rows are timer-unused; online-safe. Fresh installs get every
column from the schema files directly (no migration needed).

Participants:
- ensure every `200` carries an **object** `result`; move terminal-failure signaling out of `state` into a
  `result`;
- keep **`GET` read-only**, and make a same-`operation_id` **`PUT` reclaim/replay** an expired-working op
  (→ `200`) instead of reporting `202` forever — the coordinator now drives recovery via that re-PUT;
- update compensation handlers to the `{"forward":{…}}` envelope.

Workflow authors: convert `#` comments to `//`.

## Verification

- microflows component `just test`: 148-check stored-procedure regression + 20 e2e (base/asan/memcheck) +
  99 parser fixtures — all green.
- coordinator↔singular integration `just test`: **208/208**, including the `c12_*` case — fresh
  crash-after-commit → pending; recovered GET `202` → escalate → re-PUT reclaim → **completed**;
  `exec_count == 1` (effectively-once through reclaim); durable `redispatch_count` advanced; exactly one
  re-dispatch PUT issued.

Full per-change detail: `microflows/CHANGELOG.md`. Design: `microflows/doc/microflows_design.md` (§5.1.1).
