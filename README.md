# Drift Workflows

**Drift Workflows** is a Drift Foundation project for building and running
durable, typed workflows. It comprises two distinct components:

| Component | What it is | Where it lives |
|---|---|---|
| **Microflows** | Typed durable workflow manager/service + runtime | [`microflows/`](microflows/) |
| **Singular** | Language-neutral idempotency protocol + library (used by *participant* services) | [`singular/`](singular/) |

Both components live in this repository and **are independently buildable and testable**; they are versioned, signed, and **released only from the
repo-root `drift/manifest.json`** (the per-component manifests are local-dev, version `0.0.0`). **Microflows** is the component
furthest along; most of this README describes it. **Singular** is the
authoritative in-repository idempotency component — its normative contract is
[`singular/doc/singular-protocol.md`](singular/doc/singular-protocol.md), with
the Drift binding under `singular/drift/` and future Java/Rust/Python bindings
alongside. **Microflows does not depend on Singular**; *participant services*
use Singular for operation-level idempotency and durable outcome replay.

A thin root `justfile` delegates `test`/`stress`/`perf` to both components.

> The repository directory is historically named `phase-drift`; the project
> identity is **Drift Workflows**. The product/runtime name is **Microflows**
> throughout its package, service, protocol, and design documents.

---

# Microflows

Microflows is a typed, durable **workflow coordinator**. It composes workflows
out of typed remote operations dispatched to participant services that own
their own data, transactions, and invariants.

> Humans declare intent; the runtime drives every operation to a defined end
> state — a *definite* outcome where one exists, or a durable, explicit
> indeterminate state that is never mistaken for success or failure.

Microflows is **not** a business-record host or a database language. It
persists only its own coordination state and calls out to participants for
business effects.

> **Renamed from PhaseDrift (2026-06-07).** PhaseDrift was a workflow language
> whose runtime owned a transactional storage substrate (MILE Store). That
> local-transaction model is withdrawn; see
> [doc/microflows_design.md](microflows/doc/microflows_design.md) §"History & rationale".

## Why Microflows

Long-running business workflows — refunds, transfers, settlements, report
generation — span services that each own their own database. A process crashes
between two steps. A payment processor accepts a request and goes silent. A
retry double-submits. An operator fixes one record by hand.

Handling this by hand means every developer must remember every retry,
reconciliation, compensation, and resume path across every service boundary —
forever. Microflows moves that into the runtime: dispatch, idempotent retry,
reconciliation, durable continuation, reverse-order compensation, crash
recovery, and audited manual resolution are the execution model, not
conventions.

## Responsibility boundary

**Microflows owns** workflow definitions and pinned revisions; durable
continuations, variables, requests, and results; remote operation dispatch;
retries and reconciliation; checkpoints and reverse-order compensation;
executor leases, fencing, cancellation, recovery, and manual resolution.

**Participant services own** business data and schemas; local transactions and
invariants; operation-level idempotency; durable operation lookup; apply and
reverse behavior.

## Programming model

A workflow calls typed remote operations, persists each result durably, and
passes it to later steps. Every remote call is an implicit durable suspension
boundary — there is no `await`.

```microflows
workflow refund_parent(...) {
    val staged    = payments.stage_refund(...)
    val submitted = processor.submit_refund(staged)

    case submitted {
        Accepted(result) => settlement.reconcile(result)
        Rejected(reason) => return RefundRejected { reason: reason }
    }
}
```

Each committed remote operation produces a **Checkpoint** (its durable result).
If a later operation fails, Microflows invokes declared **reverse operations**
for completed Checkpoints in reverse order — each reverse is itself a remote
dispatch over the same protocol.

## The coordination guarantee

Microflows cannot co-commit a business effect with its checkpoint — the effect
lives in the participant's database, the checkpoint in Microflows'. But that
co-commit was only ever a *physical optimization for colocated data*; external
participants always needed idempotency and reconciliation. Microflows replaces
it with **stable logical identity and outcome convergence**, uniform across
internal and external participants:

```
stable operation ID  +  idempotent submission  +  durable status lookup
  -> duplicate dispatches cannot create a second logical operation
  -> reconciliation returns the same durable result
```

Microflows never blind-retries a remote operation as new; it re-polls by stable
ID, and the participant replays the same result. **Polling is the correctness
baseline; callbacks are an optional latency optimization.**

The honest guarantee — Microflows does **not** promise every operation
eventually becomes definite:

> Ambiguity is durable, explicit, and never mistaken for success or failure.
> An operation that the participant cannot resolve stays **indeterminate** —
> visibly, durably — until reconciliation or audited manual resolution moves
> it. A participant that is simply not ready ("busy, check back later")
> **defers** — a durable, scheduled, nonfailure outcome — rather than failing.

This is the saga guarantee, made typed, revision-pinned, and durable, with
indeterminacy as a first-class persistent state.

## Runtime guarantees

- **Durable result and continuation** — a committed operation's typed result
  and the workflow's next position are persisted together; execution resumes
  where it stopped, never by replaying history.
- **Idempotent retry** — remote operations are re-driven by stable ID; the
  participant returns the same result. Retryable, terminal, and indeterminate
  outcomes are explicit.
- **Reverse-order compensation** — on failure or cancellation, completed
  Checkpoints compensate newest-first via declared reverse operations.
- **Blocked resolution** — if compensation cannot complete, automatic unwind
  stops and the workflow waits for audited operator/service resolution.
- **Prompt cancellation** — an authorized cancel *is* the transition to
  reversal, fenced against in-flight publication.
- **Executor leases + fencing** — workers claim workflow instances with
  database-time leases and fencing tokens; every publication validates the
  lease inside its own transaction, so a stale executor cannot publish.
- **Crash recovery** — an expired lease on a claimable workflow is ordinary
  reclaimable work; recovery is not a special mode.

## Execution model

- Hot-deployable typed scripts, like stored procedures.
- Parsed, type-checked, bound, and verified into a **portable interpreted IR**
  — not native binaries.
- Running workflows are **pinned to immutable revisions**.
- Recovery **resumes from the durable continuation**, not full-history replay.
- JSON-compatible value model; schemas required at remote and durable
  boundaries; raw JSON is an explicit runtime-checked escape hatch, never the
  default.
- Variables, arrays, objects, optionals, `if`, `case`, early return, and
  deterministic local iteration / collection transforms. **No control-flow
  cycle may directly or indirectly invoke a remote operation.**

## Remote participant protocol

The contract is **observable behavior**, not an implementation. A participant
may satisfy it with [Singular](singular/) (preferred inside
PushCoin) or any other mechanism — Microflows never requires Singular at the
wire level.

Required guarantees: stable operation identity; same ID + same input → same
result; same ID + different input → rejected; durable status/result lookup;
explicit pending / deferred (busy, with a due time) / terminal-failure /
indeterminate outcomes.
Participant identity, URL, and auth come from trusted deployment config, not
workflow input.

See [doc/microflows_design.md](microflows/doc/microflows_design.md) §5 for the proposed
minimum endpoint shape (still open for redesign).

## Ownership & packaging

Microflows is a **Drift Foundation** project, aligned to the conventions of
sibling Foundation repos (`drift-web`, `drift-mariadb-client`) for repo/package
structure, manifests, signing, dependency resolution, and the standard
test/stress/perf gates.

- **Microflows** is the durable workflow/job-manager service + runtime we build.
- **Singular** is a separate reusable Drift library that *participant services*
  use for idempotency — Microflows itself never depends on it.

Both are ours and may eventually share a repository; nothing is relocated yet.

Coordinator database artifacts (purpose-built control tables + stored
procedures) are packaged in **Mariachi-compatible form** and deployed via
Mariachi — never ad-hoc loaders, and the runtime never issues free SQL. See
[doc/conventions_and_db_migration.md](microflows/doc/conventions_and_db_migration.md).

The implementation plan and current sequencing live in
[doc/microflows_design.md](microflows/doc/microflows_design.md) §7 (the design of
record); [doc/phase_drift_mile_design.md](microflows/doc/phase_drift_mile_design.md)
is the superseded PhaseDrift design, kept for historical rationale.
