# DRAFT — NOT PUBLISHED — uflowsd / microflows next-release announcement

> Internal draft for cross-team review. Do not distribute. Covers the shipped pushcoin bundle work (the
> `.mf` comment switch + #3–#5 + the `200`-result protocol hardening), plus the still-open #2 / `404` item.
> Verified on certified driftc 0.33.61 / ABI 18; full root `just test` green.

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

## Still pending (not in this release)

- **#2 — participant `404` handling.** PUT/GET `404` remains **retryable** (an infra LB/mesh/ingress
  404 is transient; a definite-abort would false-abort financial flows). The planned **durable,
  bounded reconcile budget** (count + wall-time, configurable per deployment; on expiry → a
  non-success terminal, never silent infinite pending) is **designed but NOT yet implemented** —
  tracked as an open fork (likely a coordinator-schema field). Until then, an unreachable participant
  keeps deferring/retrying.

## Breaking changes

1. **Participant `200` contract.** A `200` body **must** be `{"state":"succeeded","result":{…}}`
   with `result` an **object**. Participants that returned `200` with no `result`, a scalar/`null`
   `result`, or signaled terminal failure as `200 {state:"failed"}` must change: a business-negative
   outcome is a `200` **result** the workflow branches on; the protocol-violation cases now terminate
   the workflow `failed`.
2. **Client outcome vocabulary.** `{"workflow":"reversed"}` (exit 0) is **removed**; consumers must
   read `{"workflow":"failed","reason","compensated"}` (exit 3, HTTP 200). Read the outcome
   *document*, never infer from the HTTP status / exit code (both are advisory adapters).
3. **Compensation request body.** Reverse operations now receive the `{"forward":{…}}` envelope, not
   the bare forward input. Compensation implementations must read `forward.input` / `forward.result`.
4. **`.mf` source.** `#` comments no longer parse; `fail`, `result`, `local`, `arg` are reserved
   selector/statement heads (a bare `if result {` with an arg field named `result` still parses as
   `arg`, but `result <name>` is now a result selector).

## Migration notes

- **Coordinator schema:** apply `microflows/db/migrations/0001_terminal_failed_state.sql` — adds the
  `terminal_reason` column + the `failed`(7) state, and **backfills** legacy `reversed`(5) rows
  deterministically from the audit log (rows with `compensation_settled` events stay `reversed(5)` =
  `compensated:true`; the old empty-stack `reversed` rows become `failed(7)` = `compensated:false`),
  with documented fallbacks. Fresh installs get this from the schema directly.
- **Participants:** ensure every `200` carries an object `result`; move terminal-failure signaling
  out of `200 {state}` and into a result the workflow branches on; update compensation handlers to
  the `{"forward":{…}}` envelope.
- **Workflow authors:** convert `#` comments to `//`; opt into `case result …` + `fail` where you
  previously had a participant own the decline policy.

## Verification

- Certified driftc 0.33.61 / ABI 18. Root `just test` green: singular, microflows
  (parser fixtures, e2e, stored-procedure regression), and the coordinator↔singular integration suite
  (incl. the new result-branch / `fail` / `200`-protocol cases).
