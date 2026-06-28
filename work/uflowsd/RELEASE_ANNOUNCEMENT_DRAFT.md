# DRAFT — NOT PUBLISHED — uflowsd / microflows next-release announcement

> Internal draft for cross-team review. Do not distribute. Covers the shipped pushcoin bundle work (the
> `.mf` comment switch + #2–#5 + the `200`-result protocol hardening).
> Verified on certified driftc 0.33.63 / ABI 18; full root `just test` green.

## TL;DR

Microflows workflows can now **branch on a participant's result and author their own failure**
(`case result auth.status { … "declined" { fail "payment_declined" } }`), every definite failure
terminates with an explicit **`failed` / `compensated`** outcome, and the participant `200` contract
is **hardened** (a 200 must carry an object `result`). Several changes are **breaking for participant
services and for any consumer that parsed the old `reversed` outcome** — see *Breaking changes* +
*Migration*.

## What's new

### 1. Result-conditional branching + authored `fail` (workflow language)
- `if`/`case` selectors now accept a **path selector**: `arg <path>`, `result <name>.<path>`, or
  `local <name>.<path>` (a bare path is `arg`). `if` must be Bool; `case` branches on a scalar.
- **`fail "<reason>"`** is a new terminal statement: it durably records a machine-readable String
  reason code and enters reversal, unwinding every settled compensable step (including the op whose
  result triggered it). A reason that is statically non-String or > 190 bytes is rejected at build;
  a dynamic non-String/overlong reason fails deterministically as `invalid_fail_reason` and **still
  unwinds** (never strands a checkpointed side effect).
- Business policy lives in the `.mf`: a participant's `200` decline is a normal *result*; the
  workflow decides whether to fail. Example: `examples/workflows/payment_decline_guard.mf`.

### 2. `failed` / `compensated` durable terminal model
- A definite failure now terminates as `{"workflow":"failed","reason":…,"compensated":<bool>}`
  (CLI exit 3, HTTP 200). `compensated:true` = a real unwind ran; `compensated:false` = nothing to
  unwind (e.g. first-op rejection). Replay renders from durable state (a new `terminal_reason`
  column), never recomputed.
- **The old success-shaped `{"workflow":"reversed"}` (exit 0) is gone.**

### 3. `200` result-only protocol hardening
- A participant `200` **must** carry an **object** `result`. A 200 with a **missing** `result`
  (`participant_protocol_missing_result`) or a **non-object** `result`
  (`participant_protocol_invalid_result`) is now a definite **protocol failure** that flows through
  the `failed` terminal surface (exit 3) — it no longer crashes the coordinator (`runner-fatal`).
  Enforced on the PUT, the reconcile-PUT, and the GET-reconcile paths.

### 4. Compensation forward-context envelope
- A compensation request body is now a standard envelope:
  `{"forward":{"workflow_id","operation","operation_id","schema_version","input","result"}}` — the
  reverse op sees both the forward **input** and the forward **result** (e.g. an auth_id/reservation_id
  it must void), not just the forward input. (Replaces the old "compensation input = the forward
  input" body.)

### 5. Node-address operation ids
- An operation's stable identity is now `H(workflow_id, pinned content_hash, operation_node_id)` — a
  compiled node executes at most once per instance. Resume **adopts** the durably-stored id (never
  re-derives), preserving idempotency across recovery and migration.

### 6. `.mf` comment syntax
- `.mf` now uses **C-family comments**: `//` to end-of-line and `/* … */` (non-nesting). **`#` is no
  longer a comment** (it's a parse error). All shipped examples/fixtures migrated.

### 7. Durable bounded reconcile budget for persistent participant `404`s (#2)
- A participant `404` stays **retryable** (an infra LB/mesh/ingress 404 is transient), but a *persistent*
  route-404 — no record AND won't accept the resubmit — is now **bounded**. Each confirmed route-404
  (re-PUT 404, or GET-after-resubmit 404 — never a 202/5xx/transport blip) advances a **durable budget**
  on the operation row (forward) or checkpoint row (reverse), keyed so a resume can never reset it.
- Within budget the workflow **defers + retries**; on exhaustion — wall-time elapsed **and** a
  min-attempts floor — it enters **`blocked`**: forward (direction forward, disposition *indeterminate* —
  the op never executed) or, when a **compensation** is the one 404ing, the reverse-block path (checkpoint
  `resolution_required`). No compensation runs; the durable `participant_route_unknown` reason is carried
  in the continuation so inspect/replay renders the same `{"workflow":"blocked",…}` outcome (exit 3).
- Configurable per deployment: `reconcile_budget.{max_elapsed_ms, min_attempts}` (default **30 min / 2**),
  validated strictly at startup (no silent fallback). **No more infinite silent pending.**

## Breaking changes

1. **Participant `200` contract.** A `200` body **must** be `{"result":{…}}` with `result` an
   **object**; **`state` is not read on a 200** (optional/advisory, never required). Participants that
   returned `200` with no `result`, a scalar/`null` `result`, or signaled terminal failure as
   `200 {state:"failed"}` must change: a business-negative outcome is a `200` **result** the workflow
   branches on (via `case`/`fail`); the protocol-violation cases now terminate the workflow `failed`.
2. **Client outcome vocabulary.** `{"workflow":"reversed"}` (exit 0) is **removed**; consumers must
   read `{"workflow":"failed","reason","compensated"}` (exit 3, HTTP 200). Read the outcome
   *document*, never infer from the HTTP status / exit code (both are advisory adapters).
3. **Compensation request body.** Reverse operations now receive the `{"forward":{…}}` envelope, not
   the bare forward input. Compensation implementations must read `forward.input` / `forward.result`.
4. **`.mf` source.** `#` comments no longer parse; `fail`, `result`, `local`, `arg` are reserved
   selector/statement heads (a bare `if result {` with an arg field named `result` still parses as
   `arg`, but `result <name>` is now a result selector).

## Migration notes

- **Coordinator schema:** apply `microflows/db/migrations/0001_terminal_failed_state.sql` and
  `microflows/db/migrations/0002_reconcile_budget.sql` (the latter adds the per-dispatch
  `reconcile_*` budget columns to `tb_mf_operation` + `tb_mf_workflow_checkpoint`, NULL/0 defaults,
  online-safe). `0001` adds the `terminal_reason` column + the `failed`(7) state, and **backfills** legacy `reversed`(5) rows
  deterministically from the audit log (rows with `compensation_settled` events stay `reversed(5)` =
  `compensated:true`; the old empty-stack `reversed` rows become `failed(7)` = `compensated:false`),
  with documented fallbacks. Fresh installs get this from the schema directly.
- **Participants:** ensure every `200` carries an object `result`; move terminal-failure signaling
  out of `200 {state}` and into a result the workflow branches on; update compensation handlers to
  the `{"forward":{…}}` envelope.
- **Workflow authors:** convert `#` comments to `//`; opt into `case result …` + `fail` where you
  previously had a participant own the decline policy.

## Verification

- Certified driftc 0.33.63 / ABI 18. Root `just test` green: singular, microflows
  (parser fixtures, e2e, stored-procedure regression), and the coordinator↔singular integration suite
  (incl. the new result-branch / `fail` / `200`-protocol cases).
