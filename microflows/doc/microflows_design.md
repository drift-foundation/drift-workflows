# Microflows Design

**Status:** Working design of record (supersedes `phase_drift_mile_design.md`)
**Audience:** implementation team
**Purpose:** define Microflows as a typed, durable **workflow coordinator** —
its responsibility boundary, preserved runtime decisions, the remote
participant protocol, and the milestone-1 plan.

> Renamed from **PhaseDrift** on 2026-06-07. The prior paper
> (`phase_drift_mile_design.md`) is preserved as the historical record; see
> §10 (History & rationale) for why the direction changed and what it cost.

> **As-built note (2026-06-22).** Milestone 1 is LANDED and proven. The
> manual-IR runtime (durable dispatch, recovery, reversal) AND the `.mf`
> language frontend (parser, type binding, lowering, diagnostics) are complete
> on certified driftc **0.33.53** / ABI **18** (re-validated when the certified
> toolchain advanced 0.33.45→0.33.53; ABI 17→18 brought the wait-set I/O
> readiness path + mariadb-rpc 0.7 (whose new `PoolEvent::IdleConnRecycled` is
> handled in the singular gateway) + **web-rest 0.5.6**, which fixed a keep-alive
> epoll-readiness defect that had added ~2.3s/dispatch and blocked workflows of
> 4+ remote operations. content_hashes are graph-authoritative and unchanged
> across the ABI bump; seed fixtures stable). Verified for the latest slice
> (business-team starter kit, `microflows/examples/` — runnable payment/inventory/
> account templates over the §15 service): the `coordinator-singular`
> integration gate (**165/165**, DB-backed, real HTTP); the unit gates
> (`ir_*`/`parser_test`, base + asan) and the other component gates
> (stored-procedure, singular) were green at their slice's authoring and are
> unchanged. §§1–11 below remain the
> design of record (guarantee model,
> participant protocol, persistence, history); they describe **intent** and a
> plan whose steps are now done. **§12 documents the system AS BUILT** — the IR,
> identity, validation, the `.mf` language, and the V1 capability envelope. For
> a task-oriented authoring guide aimed at participant/workflow teams, see
> **`microflows_user_guide.md`**. Where §§4/7/8 describe the parser or
> control-flow as "deferred / after runtime proven," read §12 for what actually
> shipped.

---

## 1. What Microflows is

Microflows is a typed, durable **workflow coordinator**. It is **not** a
business-record host or a database language. It composes workflows out of
**typed remote operations** dispatched to **participant services** that own
their own data, transactions, and invariants.

> Humans declare intent; the runtime drives the workflow to a defined end
> state — a definite outcome where one exists, or a durable, explicit
> indeterminate state that is never mistaken for success or failure (§2.4).

### Responsibility boundary

**Microflows owns:**

```text
workflow definitions and pinned revisions
durable continuations, variables, requests, and results
remote operation dispatch
retries and reconciliation
checkpoints and reverse-order compensation
executor leases, fencing, cancellation, recovery, and manual resolution
```

**Participant services own:**

```text
business data and database schemas
local transactions and invariants
operation-level idempotency
durable operation lookup
apply and reverse behavior
```

A workflow calls typed remote operations, persists each result durably, and
passes it to later steps:

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

When a later operation fails, Microflows invokes declared **reverse
operations** for completed Checkpoints in reverse order. A reverse operation
is itself a remote dispatch over the same protocol.

---

## 2. Guarantee model (what changed)

### 2.1 What is removed (and what replaces it)

The prior Phase model co-committed a business effect and its Checkpoint in
**one local transaction** ("no money moved without a checkpoint; no
checkpoint without effects"). Microflows **cannot** atomically co-commit a
participant's business effect with its own Checkpoint — the effect lives in
the participant's database, the Checkpoint in Microflows'.

But this is a **physical same-database optimization, available only for
colocated internal data — not a logical guarantee that is lost.** In its
place the participant protocol provides **stable logical identity and outcome
convergence**:

```text
stable operation ID  +  idempotent submission  +  durable status lookup
  -> duplicate dispatches cannot create a second logical operation
  -> reconciliation returns the same durable result
```

Microflows therefore **never blind-retries a remote operation as new**: it
re-polls by the stable operation ID and the participant replays the same
result. Polling is the correctness baseline; callbacks are an optional
latency optimization.

**The pivot does not weaken the model for external systems** — a payment
processor or bank *always* required idempotency and reconciliation (prior
§17). By requiring **every** participant, internal or external, to implement
the same protocol (directly or through a thin wrapper), Microflows obtains
**uniform logical exactly-once behavior and durable outcome convergence
across both cases** — replacing a guarantee that only ever held for
colocated data with one that holds everywhere.

The single residual: **ambiguity remains only when a participant reports that
its own underlying effect is genuinely indeterminate** (§2.4). Co-commit
never faced this because it *owned* the effect; external participants always
did. That residual is now represented honestly rather than hidden.

### 2.2 What weakens

```text
compensation is no longer atomic or exact — a reverse operation is a remote
  dispatch subject to retry/reconcile/indeterminate. "reverse leaves a valid,
  auditable state" survives; "reverse is a local atomic transaction" does not.

the drawer/journal accounting model leaves the core — money-never-unaccounted
  -for as a runtime-enforced invariant becomes a PARTICIPANT responsibility.
  Microflows guarantees the workflow reaches a defined terminal/compensated
  end state; the payments participant owns balance.

indeterminate is first-class and unavoidable — a participant can be genuinely
  unable to answer; that surfaces as a workflow state and routes into the
  existing blocked_resolution / manual-resolution machinery.
```

### 2.3 What is gained / simplified

```text
no distributed-transaction fiction and no storage engine to build — MILE, the
  generic business-record store, rw/ro/create/journal, and record revisions
  are withdrawn.

a clean ownership seam — participants own data+transactions+invariants+
  operation-idempotency; Microflows owns coordination; each side scales and
  evolves independently.

every HARD runtime guarantee already built is coordination, not substrate, and
  survives intact: leases, fencing, durable continuations, checkpoints,
  reverse-order compensation, blocked_resolution, cancellation, recovery,
  event_ts chronological ordering, and the §24.4 time/command discipline.
```

### 2.4 The honest new guarantee

Microflows does **not** promise that every operation eventually becomes
definite. Logical identity and outcome convergence are guaranteed (§2.1); a
*definite* outcome is not, because a participant may report that its own
underlying effect is indeterminate, and that may persist **until manual
resolution**. What is guaranteed:

> Ambiguity is **durable, explicit, and never mistaken for success or
> failure**. Every operation has a recorded outcome of pending, succeeded,
> failed (terminal), deferred (nonterminal scheduling state, with a due time),
> or indeterminate — and an indeterminate operation stays indeterminate, visibly,
> until reconciliation or an audited manual resolution moves it.

This is the saga guarantee, made typed, revision-pinned, and durable — with
indeterminacy treated as a first-class, persistent state rather than a
transient to be optimistically collapsed.

### 2.5 Durability ordering (the dispatch sequence)

The order of durable writes around a remote call is load-bearing:

```text
1. Persist the operation request AND the suspended continuation ATOMICALLY,
   in one transaction, BEFORE dispatch.
     -> after a crash, recovery finds the request and reconciles by ID;
        it never loses track of an operation it may have sent.

2. Dispatch (PUT) the operation to the participant.

3. On a durably recorded SUCCESSFUL remote result, create the Checkpoint.
     -> a Checkpoint exists ONLY for an operation whose success is durably
        recorded. No checkpoint for pending, deferred, failed, or
        indeterminate operations.

4. Resume the continuation; pass the result to later steps.
```

Consequences:

```text
a participant "busy, check back later" is a durable DEFER (a nonfailure
  scheduling outcome with a due time), NOT a failure and NOT a Checkpoint.
  (Transport-level retries of an unacknowledged dispatch remain valid; that is
  distinct from the participant deferring its own work — see
  singular-protocol.md §6.7.)

a crash between (1) and (3) is safe: the request is durable, so recovery
  re-polls by stable ID; the participant replays its outcome.
```

---

## 3. Preserved decisions (carried from the prior design)

These are **unchanged** and referenced from `phase_drift_mile_design.md`
rather than re-derived:

```text
lifecycle states + transitions + dispositions ............ prior §7.1, §24.6
claimability and the claim predicate ..................... prior §24.1, §24.2
executor leases + transactional fencing .................. prior §24.3, §24.5
durable continuations (resume, not replay) ............... prior §4.1, §22.6
checkpoints + reverse-order compensation ................. prior §6, §7.1
blocked_resolution + manual resolution ................... prior §7.1, §24.6
cancellation as a direct fenced transition ............... prior §24.6
crash recovery (expired lease = claimable work) .......... prior §24.1
time/command discipline (no ambient nondeterminism,
  explicit IDs/timestamps fixed across retries,
  event_ts is the chronological order, no AUTO_INCREMENT)  prior §24.4
compilation: source -> verify -> portable interpreted IR;
  immutable script revisions; running instances pinned ... prior §22
```

What a Checkpoint now records changes in *content* but not in *mechanism*: it
captures the **durable result of a successful remote operation** (created only
after that result is durably recorded — §2.5), not a co-committed local write.

Reversal references a declared **remote reverse operation**, dispatched under
its **own distinct stable operation ID** over the same submit/reconcile
protocol.

### 3.1 Generalized blocked_resolution (finalized before the operation schema)

`blocked_resolution` represents **any point at which automatic execution
cannot safely choose a next action** — not only reverse failure. Because the
valid authorized-resolution transitions differ by the direction the workflow
was going when it blocked, an **execution direction** (`forward` | `reverse`)
is tracked on the workflow row and retained across the block.

Entry into blocked:

```text
forward   --forward op returns indeterminate-->            blocked (dir forward)
reversing --compensation indeterminate OR terminal fail--> blocked (dir reverse)
```

Authorized resolution (audited):

```text
blocked(forward) --resolve op as success (typed result)--> forward
blocked(forward) --resolve op as definitively failed-----> reversing
blocked(reverse) --resolve/retry compensation, continue--> reversing
blocked(forward|reverse) --accept audited exception------> resolved_exception
```

`resolved_exception` is a **distinct terminal state**, separate from
`reversed`: it means the workflow was terminated by an audited accepted
exception **without (full) compensation**. `reversed` is reserved for a
genuine full unwind (`UnwindComplete`). Never claim "reversed" when
compensation did not occur.

A **state/direction consistency invariant** is enforced in both
`state.drift` and the `tb_mf_workflow` CHECK: forward⇒forward, reversing⇒
reverse, blocked⇒either, completed⇒forward, reversed⇒reverse,
resolved_exception⇒either.

For each blocked operation, Microflows persists: the blocked operation,
execution direction, observation, evidence, resolver identity, stable
resolution request ID, and (for resolve-as-success) the replacement result.
These fields shape the operation/request/result tables (step 2b).

The state machine is implemented and exhaustively unit-tested in
`packages/microflows/src/state.drift`: **six** states (incl. terminal
`resolved_exception`), an orthogonal `ExecutionDirection` with the consistency
invariant, a direction-aware `transition(from, dir, cause)` authority, the
cause→disposition map (incl. a durable `indeterminate` disposition), and the
`(state, disposition)` + `(state, direction)` validity matrices mirrored by
the `tb_mf_workflow` CHECK constraints.

---

## 4. Language model (preserved)

```text
hot-deployable typed scripts
parse, type-check, bind, verify into portable interpreted IR
running workflows pinned to immutable revisions
durable continuation rather than full-history replay
JSON-compatible fundamental value model
schemas required at remote and durable boundaries
typed path traversal
variables, arrays, objects, optionals, if, case, early return
deterministic local iteration and collection transformations
no control-flow cycle may directly or indirectly invoke a remote operation
every remote call is an implicit durable suspension boundary; no await keyword
```

Raw JSON is an explicit, runtime-checked **escape hatch**, never the default.
Schemas are authoritative at remote and durable boundaries.

### 4.1 Script deployment model (milestone 1)

Microflows is a **standalone service**. Workflow scripts are hot-loadable `.mf`
**source**, compiled at runtime into in-memory portable IR — never native
binaries, and (in milestone 1) never persisted or reloaded as serialized IR.
**The deployment environment owns workflow source revisions; `.mf` source files
are the deployment artifacts.**

This model is reached in two corrections from an initial sketch: loading is
driven by an **explicit manifest** (not a directory scan), and Microflows
**compiles from source on every startup** (it does not persist/reload compiler
artifacts). The points below are the authoritative consolidation.

**Source of truth — an explicit manifest, not a directory scan.** Microflows
loads only the scripts a deployment manifest declares. Undeclared files in the
same trees are ignored, so work-in-progress scripts coexist safely.

```json
{
  "scripts": [
    { "path": "/opt/microflows/scripts/refund.mf" },
    { "directory": "/opt/microflows/scripts/settlement", "pattern": "*.mf" }
  ]
}
```

- Resolve only the manifest's declared files/directories. Normalize paths and
  reject duplicates or conflicting script identities.
- Explicit file paths are the preferred, higher-discipline form; `directory`
  entries MUST carry a restrictive `pattern`.

**Compile-on-startup; staged, atomic reload.**

```text
on startup:
  load the configured manifest; compile the ENTIRE declared set into an
    in-memory IR registry: parse, type-check, bind, verify each script.
  startup FAILS if any declared file is missing, unreadable, or fails to
    parse / type-check / bind / verify, or if identities conflict.

on SIGHUP / SIGUSR1:
  perform the same work in a STAGING registry.
  reject the entire reload if ANY declared script fails — the active registry
    is left untouched.
  atomically swap staging -> active only after the complete set succeeds.
```

The active registry is an **immutable IR registry keyed by (script name,
revision, content hash)**. Manifest + scripts are one deployment unit.

**Revision pinning (no in-flight migration in milestone 1).**

```text
new workflows pin the newly active revision.
running workflows keep their pinned revision; old IR is retained while still
  referenced.
compensation uses the checkpoint's pinned revision.
the deployment environment MUST provide every revision active workflows need;
  Microflows MUST NEVER silently substitute another revision.
```

**Identity & audit.** Content hashes identify loaded revisions (cryptographic
IR signatures are not required in milestone 1). Reload audit records the
manifest hash, every loaded script hash, and the supplied deployment / Git
commit identifier. JSON is **not** the workflow language; serialized JSON IR is
deferred.

**Recommended production deployment.** A Git-managed script repository, with
immutable release directories/worktrees named by commit and an atomic `current`
symlink switch performed *before* signalling the service (directory rename or
symlink swap — never an in-place edit of the live set). **Rollback** = point
`current` at a known-good commit and signal Microflows.

**Abstraction.** All of the above sits behind a `ScriptRegistry` interface, so a
MariaDB-backed or administrative-API implementation can replace the filesystem
manifest loader later without touching the executor.

**Sequencing.** The current **manual IR** (§8.3a) remains correct for the
feasibility slice. The parser and this filesystem manifest registry follow
**after** the executor/recovery loop is proven — see §7.

---

## 5. Remote participant protocol (proposed minimum)

> **SUPERSEDED for implementers.** This section is the original *proposal* and has drifted from as-built
> (e.g. `201`, `deferred`, and `indeterminate` here are not built). If you are implementing a participant or
> authoring a manifest, follow **`uflowsd_participant_contract.md`** (as-built, conformance-pinned) instead;
> it lists the divergences in its Appendix A. This section is kept for design rationale.

The contract specifies **observable behavior**, not an implementation.
Participants MAY satisfy it with Singular (preferred inside PushCoin) or any
other mechanism. **Microflows must not require Singular at the wire level.**

Required observable guarantees:

```text
stable operation identity
same ID + same input  -> the same logical operation / result
same ID + different input -> rejected
durable status and result lookup
explicit pending, deferred (busy, with a due time), terminal-failure, and
  indeterminate outcomes
```

### 5.1 Endpoints (proposed; open for redesign)

An **operation-visible, caller-assigned-identity** route. `PUT` expresses
idempotent creation at a caller-chosen ID; `GET` is durable lookup:

```text
PUT /microflows/v1/operations/{operation}/{operation_id}
  body: typed operation input + canonical input hash + schema version
  201/200  created or replayed (same id + same input hash) -> current state
  202      accepted / in-progress
  409      same operation_id, DIFFERENT input hash  (idempotency conflict)
  400      invalid input

GET /microflows/v1/operations/{operation}/{operation_id}
  200  terminal     { result: {…} }   (terminal SUCCESS; `result` is the contract, `state` advisory)
  202  pending      { state: pending }
       deferred     { state: deferred, not_before: "..." }   (busy, due time)
  404  unknown      participant has NO record of this operation
  result-only: a 200 carries an object `result`; a business-negative outcome is a RESULT the workflow
  branches on (via case/fail) — never `state:"failed"`. `state` is advisory, not interpreted on 200.
```

`{operation}` is the static operation name; `{operation_id}` is the
Microflows-assigned stable identity (§5.2).

**404 during reconciliation** means the participant has **no record** of the
operation — it never received it (or never durably stored it). Microflows MAY
then **safely resubmit the identical request under the same ID**: idempotent
creation makes a genuine first execution and a re-creation indistinguishable.
(A 404 is therefore *not* a failure — it is a "not yet" that the dispatch gap
permits.)

A **persistent** 404 — the participant has no record AND will not accept the
resubmit — is bounded by a **durable reconcile budget** (per deployment:
`reconcile_budget.{max_elapsed_ms, min_attempts}`, default 30 min / 2 attempts).
Each *confirmed* route-404 (a re-PUT 404, or a GET-after-resubmit 404 — never a
202/5xx/transport blip) advances the budget on the durable operation row
(forward) or checkpoint row (reverse), keyed so a resume can **never** reset it.
Within budget the workflow defers and retries; on exhaustion — wall-time elapsed
**and** the min-attempts floor met — it enters `blocked_resolution`. Forward:
direction forward, disposition *indeterminate* (the op never executed, so the
outcome is uncertain, not failed); reverse (a compensation 404): the existing
reverse-block path, checkpoint `resolution_required`. No compensation runs, prior
checkpoints are untouched, and the durable `participant_route_unknown` reason is
carried in the `continuation` so inspect/replay renders the same
`{"workflow":"blocked",...}` outcome.

#### 5.1.1 PUT owns reclaim; GET stays read-only (crash-after-commit recovery)

A participant that **commits its side effect and crashes before recording the
terminal result** (e.g. Singular `complete`) leaves the operation *working* with
an unexpired lease. Its `GET` then answers **202 pending** — correctly, since
there is not yet a durable terminal result to return. Recovery of this case is
**coordinator-driven via PUT**; the participant contract makes the asymmetry
explicit:

- **`PUT {operation_id}` with the same input is an idempotent reassert/reclaim**,
  never a fresh execution. A **live-working** operation (lease unexpired) ⇒ the
  PUT returns **202** and the live lease is **never stolen**. An
  **expired-working** operation (lease expired ⇒ the prior worker is fenced) ⇒
  the PUT **MUST reclaim** the operation (with Singular: `resume` granting a
  fresh attempt + rotated token), **rerun the body idempotently** (replaying the
  already-committed effect, not re-applying it), and **complete / replay** the
  recorded result — returning **200**.
- **`GET` is read-only and NEVER owns reclaim.** It returns terminal | pending |
  unknown only; it never mutates the operation's lease or attempt state. A
  crashed-mid-commit operation therefore stays 202 under `GET` until a `PUT`
  reclaims it — so the coordinator must re-PUT, not poll forever.

Because `GET` cannot make progress on a committed-but-uncompleted operation, the
coordinator escalates a **recovered** operation that a `GET` reports as a
**confirmed** pending (a 202 — *not* a 5xx/transport blip) toward a
byte-identical re-PUT, bounded by a **durable, fenced re-dispatch timer** (per
deployment: `pending_redispatch_after_ms`, default 60000 ms; SHOULD be ≥ the
participant lease TTL + margin). The timer lives on the durable operation row
(forward) / checkpoint row (reverse), keyed so a resume can **never** reset its
epoch. Within the interval the workflow defers and re-polls; once elapsed it
**re-PUTs under the held lease** — a live participant answers 202 (re-armed,
escalates again next interval), an expired one reclaims → 200. Unlike the 404
reconcile budget there is **no exhaustion/block**: a re-PUT is idempotent and
safe, so this escalates indefinitely (a genuinely broken op fails *definitively*
via the rerun's 400 → reversal; a slow-but-alive op keeps answering 202).

### 5.2 Properties

```text
STABLE OPERATION ID is derived, never user-controlled, from:
    workflow instance id  +  pinned script revision  +  static call site
    +  invocation identity (which logical occurrence of that call site)
  -> the same logical step in the same pinned workflow instance always
     yields the same operation_id, across retries and recovery, with no
     coordination and no participant round-trip.

CANONICAL INPUT HASH + SCHEMA VERSION accompany the request. Same operation_id
  with the same input hash is idempotent replay; same id with a DIFFERENT
  hash is a contract violation -> 409. (Catches a workflow-logic or
  revision-mismatch bug where one logical step would submit two inputs.)

participant identity, operation name, URL, and auth profile come from TRUSTED
  deployment configuration, never from workflow input.

polling is the correctness baseline; callbacks are an optional optimization
  (POST to a Microflows-supplied URL on terminal; never required).

outcomes are explicit. a participant "busy, later" is a durable DEFER (a
  nonfailure scheduling outcome with a due time), distinct from terminal
  failure and from an indeterminate result.

compensation is just another operation, with its OWN distinct stable
  operation_id, over the same submit/reconcile protocol.
```

### 5.3 Microflows-owned dispatcher

Microflows MAY own a generic REST dispatcher that understands transport,
scheduling, retries, reconciliation, and **normalized outcomes** — and
contains **no payment/report/domain logic**.

### 5.4 Open for redesign

The endpoint shape and envelope are **not** a compatibility target with the
Java Microflows / Bookkeeper prior art. Where a cleaner typed/reconcilable
design warrants it (e.g. typed result variants in the envelope, a normalized
indeterminate sub-taxonomy), we change it. Conformance tests pin the
observable contract.

---

## 6. Persistence (runtime/control state only)

MariaDB persists **only Microflows runtime/control state**, via purpose-built
schema and stored procedures using the existing `mariadb-rpc` client. SPs
receive explicit IDs and timestamps, avoid ambient nondeterminism, and
implement reproducible, state-idempotent transitions (prior §24.4).

**Time direction (milestone-1 TimeSource).**

```text
sp_mf_clock_read is Microflows' TimeSource; transition SPs receive fixed
  timestamps explicitly and NEVER call NOW() (future deployments may use
  NTP-synchronized local clocks — not implemented in the spike).
each workflow retains current_event_ts; a transition requires
  new_event_ts > current_event_ts.
a non-increasing timestamp indicates CLOCK SKEW: abort and DEFER the workflow
  (do NOT adjust the timestamp or immediately retry).
event_ts is the workflow-local ordering: strictly increasing per workflow
  (enforced by the guard above), so history is ordered by chronology alone.
```

```text
workflows and definition revisions
continuations and durable values
operation requests and results
checkpoints and reversal state
attempts, leases, and fencing
events, cancellation, and manual resolution
```

**Removed:** the generic business-record store (`tb_pd_record`) and its
`rw`/`ro`/`create`/`journal`/record-revision model; the accounting journal
(`tb_pd_workflow_journal`) leaves the core (participant-side now). The pending
mariadb-rpc free-SQL (`exec`/`CLIENT_FOUND_ROWS`) work is **withdrawn** —
Microflows calls only stored procedures.

DB artifacts are packaged in **Mariachi-compatible form** (the Foundation
schema-orchestrator), not loaded ad hoc:

```text
db/{schema,procs,constants,grants,scenarios}/ canonical layout
tb_mf_<name>.sql  — CREATE TABLE IF NOT EXISTS; FKs named (fk_mf_*)
sp_mf_<name>.sql  — deterministic, state-idempotent SPs
deploy/test via `mariachi apply` (no free SQL in runtime; no bash loader)
```

The concrete Mariachi layout and our migration steps are in
`conventions_and_db_migration.md` (reported for confirmation before the DB
files are restructured).

---

## 7. Milestone-1 implementation plan

The **protocol spike (§8) moves ahead of the operation schema.** Its concrete
requirements — exactly what a request, an outcome, and a reconciliation need —
shape the request/result tables, instead of finalizing those tables
speculatively. Only the spike-independent control-state changes happen first.

```text
1  Workflow state machine + claimability ............ DONE (reused as-is)
2a Schema: REMOVE record + journal stores ........... DONE
3  Lease acquisition + transactional fencing ........ DONE (reused as-is)
8  Minimum REST participant protocol + feasibility
     spike .......................................... DONE (conformance ref)
2b Schema: operation request/result + definition
     tables ......................................... DONE (shaped by the spike)
     (atomic request+continuation persist; input hash + schema version;
      normalized outcome incl. durable indeterminate)
4  Durable remote-operation dispatch + result
     persistence, with manual IR .................... DONE (graph-driven; §12)
5  Crash recovery + idempotent resumption ........... DONE (replay; §12.2)
6  Reversal + blocked_resolution
     (reverse = remote compensation dispatch) ....... DONE
9  Generic REST dispatcher .......................... DONE (config routing)
7  Parser + type checker ............................ DONE — the .mf frontend
     (whole V1 IR + diagnostics; §12.6)
10 Script deployment: manifest-driven ScriptRegistry
     (compile-on-startup + staged SIGHUP reload) .... see §4.1 (registry built;
     manifest loader is the remaining packaging step)
```

> **Status (as-built, 2026-06-19):** every step above is landed and proven
> except the manifest-driven filesystem ScriptRegistry packaging (§4.1) — the
> runtime today loads a config-supplied revision (`--config` + `--lower-source`).
> See **§12** for the as-built runtime + language.

**Script deployment (§4.1):** milestone 1 compiles `.mf` source from an explicit
manifest into an in-memory IR registry on startup, with staged atomic
SIGHUP/SIGUSR1 reload; no serialized IR is persisted. It follows the parser
(step 7), which follows a proven executor/recovery loop. The manual IR (§8.3a)
remains the spike's loader until then.

The code/schema rename (`phasedrift`→`microflows`, `tb_pd_`/`sp_pd_`
prefixes) folds into step 2a/2b and the next code touch, rather than a
separate churn pass — see Open Questions.

---

## 8. Feasibility spike (gate before the dispatcher)

### 8.1 Persistence split (decided)

```text
Microflows coordinator  -> its OWN MariaDB schema + stored procedures via
                           mariadb-rpc. It does NOT use Singular.

Participant services     -> MAY use Singular for idempotency, durable outcome
                           replay, and reconciliation.

Reference participant stub -> USES Singular. This validates the expected
                           PushCoin participant pattern end-to-end while
                           keeping Singular entirely outside Microflows.
```

### 8.2 Reference participant stub (the conformance reference)

A narrow Drift HTTP service implementing only §5.1, backed by Singular for
durable idempotent state. It **becomes the protocol conformance reference**;
real Bookkeeper integration can follow later as a participant or adapter test.

```text
PUT and GET only
stable operation id + canonical input-hash validation
same request (same id + same hash) replays the same logical result
different input under the same id -> 409
configurable outcomes: pending, success, terminal failure, deferred
  (busy, with a due time), indeterminate
delayed response and drop-after-commit, for lost-ack tests
durable state across participant restarts
```

The stub maps the protocol onto Singular: the stable operation id is the
idempotency key; `start`/`inspect` provide claim + dedup + durable replay; the
stub's configured outcome is its "business" result, stored in Singular and
replayed verbatim. Indeterminate is the participant reporting its own effect
is indeterminate — stored durably and surfaced by `GET` as
`state: indeterminate` (§2.4), never collapsed to success/failure.

**Responsibility split at the boundary (decided):**

```text
Singular (participant-side) owns: operation identity, claim/deduplication,
  execution, and durable OPAQUE outcome replay.

Microflows (coordinator) owns: accepted/pending state, polling, retry
  scheduling, and normalized workflow state.

indeterminate is an exceptional participant OUTCOME PAYLOAD, not a Singular
  lifecycle state. Singular stores and replays it opaquely; the PARTICIPANT
  maps it to the REST protocol's indeterminate state. Microflows then persists
  it, stops automatic progression/compensation, and requires reconciliation
  or audited manual resolution (§3.1). No additional participant-owned
  lifecycle store is needed for the spike unless implementation uncovers a
  concrete requirement.
```

### 8.3a Spike uses manual versioned IR (no frontend)

The spike **bypasses the language frontend entirely** — no parser, source
syntax, type checker, or compiler. A manually constructed, already-validated
versioned IR workflow is the fixture:

```text
workflow: protocol_spike   revision: 1
steps:
  1. call participant.echo_transform
  2. bind result to `echo_result`
  3. complete with `echo_result`
```

This isolates the hard runtime contract: durable operation request; suspended
continuation; stable operation id; HTTP dispatch + reconciliation; persisted
typed result; Checkpoint creation; resume + completion; crash recovery. The
minimal stack:

```text
manual IR fixture -> Microflows executor/dispatcher -> HTTP
  -> Drift web-rest participant stub -> Singular
```

The participant is **Drift** (validates the intended production stack:
web-rest, Singular integration, config, durable replay). **Python is only the
black-box process/fault-injection harness.**

The fake operation:

```text
operation: echo-transform
input:  { "values": [1, 2, 3] }   (validated: values present, array, all numeric)
result: { "sum": 6 }
```

Effectively-once **execution** is proven by **observable instrumentation**, not
by a literal in the result document: the stub increments a process-level
execution counter only when the operation body actually runs (the Singular
`start()`=Granted branch), and exposes it at `GET /debug/exec-count`. The
harness submits the same operation repeatedly and asserts the counter stays
`1` — distinguishing genuine effectively-once execution from mere result
replay. (Status of the result document on replay is "durable replay verified".)

The participant stub is exercised by a checked-in black-box conformance
harness (`participant-stub/tests/http/conformance.py`, `just test-http`):
first-submit + replay + exec-count==1, input-conflict 409 (via Singular
`item_meta`), reordered-keys-no-false-conflict (canonical lex-ordered hash),
invalid-input→400-creating-no-operation, unknown-operation→400, GET
terminal/404. 6/6 passing.

### 8.3b What the loop must prove

**First runnable slice (do this first; does NOT block on the full Singular
target protocol).** It proves the central dispatch-gap recovery property:

```text
manual IR -> persist operation -> HTTP PUT -> LOST response
          -> GET by stable ID -> persist success -> Checkpoint -> continue
```

It uses only Singular 0.4 `start`/`complete`/`inspect` (no reclaim, no defer).
The participant stub answers `start`+`complete`+status-replay; Microflows
persists the request+continuation before dispatch, loses the PUT response,
recovers by GET on the stable operation id, records the success, creates the
Checkpoint, and continues. This is the success + lost-ack pair.

**Input-conflict (409) reuses Singular's `item_meta` — no second store.**
Singular 0.4 does not *compare* input identity, but it does *store* `item_meta`
on `start()`. So the stub does not create a separate hash table (which would be
a non-atomic second authority). Instead:
- on `start()`, the stub puts the **canonical input hash** into Singular's
  `item_meta`;
- on `Exists`, the stub reads the **original** `item_meta` back via
  `history()` (or `inspect()`) and compares the hash; mismatch ⇒ **409**.

The single authority for the bound input stays inside Singular. When Singular
implements input-identity conflict natively (§0.3), the stub delegates the
comparison entirely.

The minimal end-to-end scenario (real HTTP boundary):

```text
1.  start MariaDB with Microflows AND Singular schemas
2.  start the Drift participant stub
3.  submit the one-operation manual-IR workflow (§8.3a) to Microflows
4.  Microflows atomically persists the operation request + suspended
      continuation (BEFORE dispatch)
5.  Microflows PUTs to the participant
6.  participant claims/dedups via Singular, initially returns 202 Accepted
7.  Microflows polls GET
8.  participant returns the Singular-replayed success result
9.  Microflows atomically records the result, creates the Checkpoint, advances
      the continuation, completes the workflow
10. re-submit/recover -> verify the operation was logically executed once
```

Required fault cases:

```text
same operation id + same input              -> same result
same id + DIFFERENT input                    -> 409
drop the PUT response AFTER participant commit -> Microflows recovers via GET
kill Microflows AFTER persisting request, BEFORE dispatch -> recovery dispatches
kill Microflows AFTER success, BEFORE local settlement    -> recovery polls,
                                                             settles once
participant returns 404                      -> Microflows safely repeats PUT
participant returns indeterminate            -> workflow enters blocked_resolution
a compensation operation                     -> distinct stable id, same protocol
```

**First test scope:** the **success and lost-ack** paths only (the first
runnable slice above). Add **404-reconcile**, **indeterminate** (opaque
Singular result the participant maps), and **compensation** next — they
validate the state-machine decisions (§3.1). The **kill-after-success** and
**worker-crash reclaim** fault cases depend on Singular **reclaim/defer
extensions** (`singular-protocol.md` §0.3) and follow once those land.

Whatever a request, an outcome, and a reconciliation actually need here
becomes the shape of the operation request/result tables (§7 step 2b). If the
lost-ack or 404-reconcile path is awkward, we learn it cheaply before building
the dispatcher or finalizing the schema.

---

## 9. Ownership, packaging, and reference projects

### 9.1 Ownership boundary

```text
Singular   — a reusable Drift LIBRARY (peer of drift-web / mariadb-client),
             consumed as a versioned package. Microflows does NOT use it;
             PARTICIPANT services do.
Microflows — the durable workflow/job-manager SERVICE + runtime we build.
```

Both are ours and may eventually share a repository; **nothing is relocated
now**. We retain the existing external Singular dependency and keep the
package boundaries explicit. See `conventions_and_db_migration.md` §0.

### 9.2 Convention authorities

```text
../drift-web, ../drift-mariadb-client .. Drift FOUNDATION conventions:
                           repo/package structure, manifests, signing/trust,
                           dependency resolution, test+stress+perf gates,
                           ignore/generated conventions. Where these differ
                           from PushCoin/Singular, prefer Foundation.
../mariachi .............. canonical DB-artifact protocol (schema/procs/
                           constants/grants layout, deploy workflow)
../pushcoin/bookkeeper ... practical MariaDB-access + Mariachi-consumer +
                           HTTP-service reference (and the spike's HTTP/Singular
                           wiring template)
../drift-lang ............ HISTORICAL inspiration ONLY (early language/IR
                           pattern study). SUPERSEDED by the Frontend-reuse
                           policy (§12.6): Microflows NEVER depends on this or
                           any sibling checkout — not as a build dep, not as a
                           test path. The parser/lowering/diagnostics are local
                           to parser.drift. (doc/drift_lang_reuse.md is marked
                           superseded.)
../pushcoin/singular ..... build/test conventions (older PushCoin variant);
                           AND the preferred participant-side idempotency impl
                           (NOT used by Microflows directly)
../pushcoin/microflows ... protocol history + operational lessons (prior art,
                           not a compatibility target)
```

Foundation-alignment and Mariachi-migration steps (with required
confirmations) are detailed in `conventions_and_db_migration.md`.

---

## 10. History & rationale

### 10.1 The pivot (2026-06-07)

The project began as **PhaseDrift + MILE**: a workflow language whose runtime
owned a transactional storage substrate. A Phase was one atomic local commit
of business records + journal + checkpoint, with `rw`/`ro`/`create`/`journal`
guards giving optimistic concurrency over a generic business-record store
(`phase_drift_mile_design.md` §§10–19, §23.1).

It was renamed **Microflows** and narrowed to a **coordinator**: it
orchestrates typed remote operations on participant services that own their
own data and idempotency. Microflows persists only its own control state.

### 10.2 Why

The local-transaction model's strongest property — atomic co-commit of effect
and checkpoint — only held while the runtime owned *everything*. The prior
design already conceded (prior §17) that any real external party (a payment
processor, a bank) breaks that property and must be modeled as a staged,
idempotent, mirrored phase machine. Once the dominant real workflows are
exactly those — payments, settlement, report generation across independently
owned services — the local-substrate guarantee buys little and costs a great
deal: a bespoke storage engine (MILE), a generic record store, and a
relational binding layer Microflows would have had to own and operate.

The coordinator model makes prior §17 the *whole* model. It trades a strong
local-only property for **composable, durable coordination across
independently-owned services** — the property that matches how PushCoin
actually deploys.

### 10.3 What changed (carried forward as risks)

```text
the same-database co-commit of effect+checkpoint is removed — a PHYSICAL
  optimization for colocated data, replaced by stable logical identity +
  outcome convergence that holds uniformly for internal AND external
  participants (§2.1). Not a weakening; the external case always needed it.

compensation is a remote operation (own stable id), reconciled not atomic;
  reverse ending indeterminate/terminal-failure -> blocked_resolution (§2.2)

balance/accounting invariants move to participants; Microflows guarantees
  workflow end-state, not bookkeeping correctness (§2.2)

residual ambiguity = participant-reported indeterminacy of its OWN effect;
  durable, explicit, may persist until manual resolution (§2.4)
```

### 10.4 Superseded specifics

```text
MILE Store (durable record engine, write-then-publish, commit objects,
  replication paper) ......................... withdrawn
generic business-record store + rw/ro/create/journal + record revisions
  (prior §23.1) .............................. withdrawn
accounting journal in the core (drawers/dr/cr) .. moves to participants
mariadb-rpc free-SQL (exec/CLIENT_FOUND_ROWS) request .. withdrawn
```

---

## 11. Open questions

The operation request/result table shape is deliberately **not** an open
question to settle now — it is an *output* of the spike (§7, §8).

1. **Code/schema rename timing.** Fold `phasedrift`→`microflows` and
   `tb_pd_`/`sp_pd_`→`tb_mf_`/`sp_mf_` into step-2a/2b and the next code
   touch (recommended — avoids a churn pass), or do a mechanical rename
   first? The state machine, lease SPs, and host gateway are otherwise
   untouched by the pivot.
2. **Envelope typing.** Do operation results cross the wire as typed variants
   (`Accepted(...)`/`Rejected(...)`) encoded in the envelope, or as opaque
   JSON validated against the operation's declared schema on receipt? Affects
   the dispatcher and the participant contract. (Spike will exercise both
   ends.)
3. **Indeterminate sub-taxonomy.** Single `indeterminate` state, or a
   normalized set (e.g. `unknown-after-submit`, `timeout`, `participant-
   unreachable`) that drives different reconciliation policy? Indeterminacy
   is durable until resolution either way (§2.4).
4. **Reference participant for the spike.** *Resolved:* a minimal Drift stub
   implementing only §5.1, backed by Singular (§8.1–8.2). It becomes the
   conformance reference; Bookkeeper integration follows later.

---

## 12. As-built: the proven V1 runtime + language (2026-06-19)

This section is the **authoritative record of what shipped**. It supersedes the
"deferred / after runtime proven" framing in §§4, 7, and 8 for everything it
covers. The work landed incrementally on certified driftc 0.33.42 / ABI 17. Each
slice gated on the full root `just test` (unit: `ir_exec_test`, `ir_graph_test`,
`parser_test`, base + asan; stored-procedure; singular; integration:
`coordinator-singular`, real HTTP against the Singular-backed participant). The
latest LANGUAGE/FRONTEND slice (expression object/array construction, §12.9) was
re-verified DB-backed at **138/138** plus `ir_exec_test`/`parser_test` base + asan;
the latest RUNTIME slice (operational admission, §13) brought the integration gate
to **142/142**. The runtime and language live in
`microflows/runner/src/` (`ir.drift`, `parser.drift`, `runner.drift`); nothing
here depends on `../drift-lang` or any sibling checkout (§12.6).

### 12.1 The IR — a typed control-flow graph (`ir.drift`)

A workflow is a **flat node table keyed by a stable id**, with node-id edges
(not nested nodes) — so the structure is finite and needs no `Box` for control
flow. Node kinds:

```text
NOperation  a remote call — the ONLY durable suspension point
NLet        a pure value binding
NIf         a two-way branch on a Bool
NCase       an N-way branch on a scrutinee, matched against constants + default
NLoop       a finite array transform (map/filter/fold) — never a `while` cycle
NMerge      an SSA phi: rejoin branches, binding a value selected by the taken path
NReturn     terminal
```

Expressions are a **closed leaf set** (`IrExpr`), each path-projectable:

```text
EConst(canonical_json)   a pinned constant (canonical JSON literal)
EArg(path)               a durable argument, projected
EResult(node, path)      a prior NOperation's settled result, projected
ELocal(name, path)       a let / loop-element / accumulator / merge binding
```

Loop kinds are `LMap` / `LFilter` / `LFold` over a finite collection; the body
is a **pure `IrExpr`**, which structurally forbids a remote op inside iteration.

**The load-bearing invariant — the pure/durable boundary.** Only `NOperation`
is a durable boundary: it persists a request + suspended continuation *before*
dispatch and a result + Checkpoint *after* success (§2.5). Every other node —
branch, let, loop, merge — writes **no continuation and no event**. On restart
the interpreter **re-derives the pure path deterministically** forward from the
last settled operation (durable args + settled results + recomputed locals),
then reconciles the next operation against durable state. This realizes §4's
"every remote call is an implicit durable suspension boundary" and "no
control-flow cycle may invoke a remote operation," and keeps the durable model
exactly as proven for the flat plan: a control-flow construct never introduces a
new durable record. **Determinism is structural** — the IR exposes no
clock/random/env/fs/net/live-config, so the only inputs to a replay are durable
facts.

### 12.2 Identity — graph-authoritative `content_hash`

A loaded revision's identity is:

```text
content_hash = graph_canonical(graph)
             ‖ per-NOperation resolved bindings (participant + schema_version
               + compensation binding)
             ‖ canonical(argument_type)
             ‖ operation input/result + compensation type contracts (when present)
```

`graph_canonical` is a deterministic, length-prefixed encoding that **normalizes
away non-semantic variation**: nodes sorted by id, `NCase` arms sorted by
`match_const`, `NMerge` sources sorted by `from`, and every constant input
canonicalized (key-ordered compact JSON). So source formatting, key order, and
declaration order **never** change identity; a genuine semantic change always
does. Revision pinning (§4.1) is enforced on this hash: resume requires an
exact `(plan_version, content_hash)` match, else `revision_unavailable` — never
a silent substitution. The runner exposes `--emit-content-hash` (DB-free) as the
single source of the value (no reimplementation in tests or fixtures).

### 12.3 Durable arguments

Per-instance arguments are the **one** durable child the language added: a small
immutable per-`workflow_id` record holding the **canonical args document as
ordered-key compact UTF-8 bytes**, written atomically with the workflow + plan
pin. Submitted `--arguments` are validated against the declared `argument_type`
and canonicalized before creation. Reusing a `workflow_id` with **different
canonical bytes → `workflow_conflict`** (decided by `VARBINARY` byte equality,
never collated text; reordered keys with the same content are idempotent).
Resume reads the **durable** args, never the CLI/submission. The declared
argument **type** is in `content_hash` (a contract change is a new revision);
only per-instance **values** are excluded (a value change is a conflict, not a
revision). Pure control flow still persists nothing else.

### 12.4 Validation — at registry build, before any dispatch

A malformed source or config fails at **build time** → `invalid_config`
(submission, pre-claim) or `revision_unavailable` (resume, post-claim) — never a
dispatch, never new durable state. Two layers:

**Structural** (`validate_graph`): strict node/expression parsing (each kind has
exactly its allowed keys; an expression is exactly one variant — no silent
drop); **balanced operation depth** across all branches (a uniform per-path
operation count, so the durable seq = execution position invariant holds);
`EResult`/`ELocal` **dominance**; `NMerge` **totality + unambiguity** (one source
per immediate predecessor, each value available after its predecessor);
**reversibility** (every non-final operation has a compensation binding, so
reversal can never strand); loop `elem ≠ as` and finite source.

**Typed** (`type_check_graph`, optional contracts): operations may declare
`input_type` / `result_type`; a topological pass types binders before use with
three-way inference — `Known` (assignability-checked), `Unknown` (only from an
untyped op result — the backward-compatible escape hatch), and `Imprecise` (a
const whose precise type is undetermined — never permissive: a static const is
checked by value, an imprecise const reaching a typed input through a
non-static path is rejected). A **compensation** receives the standard
forward-context envelope (`{forward:{input,result,…}}`), NOT the forward op's input directly, so its
declared input type is **not** cross-checked against the forward op's (structural/opaque v1); type tags
still fold into `content_hash`. An untyped config behaves exactly as before (every contract is optional).

### 12.5 Execution & reversal (unchanged in mechanism, generalized to the graph)

Forward execution is **advance-driven**: gather durable args + settled results,
then loop the interpreter — `NeedOperation` (recover-or-derive the request,
resolve binding, request-before-dispatch, settle, checkpoint) until `Completed`
(report the final settled op's result) or `Fault` (defer). Branch/loop/let/merge
advance purely between operations. Reversal is **checkpoint-stack driven**
(`reverse_head`) and reads compensation from the operations registry — it never
consults the graph, so it compensates exactly the taken path, highest-seq first.
A forward failure after a taken branch reverses only that branch's checkpoints +
shared downstream ops; untaken branches have no operation row.

### 12.6 The `.mf` source language (`parser.drift` + `--lower-source`)

The textual frontend **lowers `.mf` source into the exact config the IR already
executes and hashes** — no new IR nodes, execution paths, or durable state. The
acceptance criterion is **lowering parity**: a parser-lowered config and a
hand-authored config produce the identical `--emit-content-hash` and execute the
same.

```text
args  { name: <type>, … }                  -> the closed-object argument_type
op    <name> { input: <T>  result: <T> }   -> operation input/result contracts (both optional)
steps { <statement>* }                     -> a flat "plan" (const-only straight line)
                                              or a control-flow "graph" (anything else)

statement := <op> <expr>                              a remote call (operation step)
           | let <name> = <expr>                      a pure binding (NLet)
           | let <name> = <op> <expr>                 a NAMED op; <name> aliases its result
           | let <name> = (map|filter) <src> each <e> <body>
           | let <name> = fold <src> from <init> each <e> <body>
           | if <selector> { … } [ else { … } ] [ merge <n> = <e> | <e> ]      (selector = arg/result/local path; -> Bool)
           | case <selector> { (<json> { … })* default { … } } [ merge <n> = <e> | … | <e> ]
           | fail <string-reason>     (authored terminal failure -> reversal; reason is a String code, <=190 bytes)
expr      := { …json… } | const <json> | arg <path> | local <name>[.path] | result <name>[.path]
type      := int | float | bool | string | null | { field: T, … } | [T] | T?
```

`merge` / `map` / `filter` / `fold` are **contextual keywords** (an op literally
named `merge` is unaffected). Result aliases live **only in the parser** (a
global symbol table mapping name → node id), so `result` refs are stable under
reformatting and alias-rename — the emitted graph and hash are identical.
`--lower-source <wf.mf> --config <base.json>` merges the source over the trusted
**deployment routing** (participants + operation→participant bindings) and runs
the **real build/validation path DB-free** before printing, so an invalid
source/config (unknown op, op-imbalanced branch, missing `case` default,
undominated/cross-branch `result` ref, non-predecessor merge source, non-array
loop source, `elem`/`as` collision, type-contract mismatch, …) fails **at
lowering**, before anything durable. The printed config is itself runnable and
`--emit-content-hash`-able unchanged.

**Diagnostics.** A parse failure is a **structured `ParseError`**: a stable,
kebab-case, machine-readable `code` (`unknown-keyword`, `expected-expression`,
`unterminated-block`, `case-arm-after-default`, `case-merge-arity`, …), source
position (`byte_offset` / `line` / `column`, 1-based, column counts UTF-8
scalars), and `expected` / `found` where applicable. `render_diagnostic` formats
a human CLI string (code + line/column + caret); `--lower-source` emits the
structured event through `std.log` (machine-parseable fields, same facility as
the bookkeeper service) **and** prints the human render.

**Frontend-reuse policy (settled).** The parser, lowering, AND diagnostics stay
**local to `parser.drift`**. Microflows **never** depends on `../drift-lang` or
any sibling checkout — not as a build dependency, not as a test path. An
instructive Drift-frontend pattern may be read as inspiration and **manually
cloned with local ownership**; if reuse pressure becomes substantial, the move
is to **ask the compiler team to extract a supported, versioned package** —
never to reach across a sibling-repo path.

### 12.7 V1 capability envelope — and the explicit limits (the hand-off boundary)

**Supported and proven:**

```text
durable saga: call a remote op, settle, CHECKPOINT, COMPENSATE on failure,
  resume after crash, effectively-once execution (re-poll by stable id)
branch & route: if / case on a durable-argument or result value
data flow: pass values between steps via arg / result / local, with path projection
named results + cross-branch merge (NMerge phi) feeding shared downstream work
finite PURE collection transforms: map / filter / fold (projection/selection)
typed contracts: optional per-operation input/result types, checked before dispatch
workflow composition: a single async `call child@<version> {…}` per step, typed
  args in / return out, awaited like any other durable step; a completed
  child's own checkpoint reverses via reverse-child compensation if the parent
  later unwinds — no fan-out, see §16
```

**NOT in V1 (deliberate — design accordingly):**

```text
in-workflow COMPUTATION — IrExpr is const/arg/result/local projections ONLY.
  No arithmetic, no string building, no comparison beyond `case` exact-match.
  => every COMPUTED value must be produced by a participant.
remote ops INSIDE a loop — loop bodies are pure; no dynamic fan-out
  ("charge each of N items"). Model a batch as one participant op, or unroll.
workflow call FAN-OUT — `call` is one child per step; no "call N children and
  gather" (deferred, see §16). `on failed` / failure-as-data for a call is also
  deferred — a child that terminates without completing (rejected, reversed,
  or failed) always drives the parent's own reversal, never a value the
  parent's script can branch on. (A non-terminal/blocked child does not drive
  reversal; it keeps the parent pending — see §16.2.)
`while` / unbounded loops — loops are finite array transforms only.
AUTHENTICATION — participant `auth_profile` must be null/absent (any value is
  rejected at build). Fine for trusted-network participants; a production
  blocker for calls over the open wire. (See security_model.md.)
variable per-branch operation counts — op-depth must be uniform across branches.
```

**The design consequence — thin orchestrator, smart participants.** Because
every computed value and every side effect lives in a participant, Microflows
stays a pure coordinator (consistent with §1's responsibility boundary).
Workflow authors orchestrate: call typed operations, branch on their results,
carry values across joins, compensate on failure. A feature that needs
in-workflow *computation* (arithmetic, comparison) is a real language extension
(a typed expression sub-language), deferred beyond V1.

**Value construction — LANDED (§12.9).** An operation input can be built from
multiple dynamic parts (`{ customer: arg c.id, amount: arg o.amount }`,
`[ arg a, const 3 ]`) via the `EObject`/`EArray` expressions. Authors wire inputs
directly; no pre-shaping of the arguments document is required. (Fully-constant
literals still fold to `EConst`, so existing hashes are unchanged.) The remaining
hard limit is in-workflow *computation* (arithmetic/comparison) — that lives in
participants.

### 12.8 Source layout (where the as-built lives)

```text
microflows/runner/src/ir.drift       typed value model, graph node/expr types,
                                       graph_canonical, validate_graph,
                                       type_check_graph, the interpreter (advance)
microflows/runner/src/parser.drift   the .mf lexer/grammar/lowering + diagnostics
microflows/runner/src/runner.drift   registry build, content_hash, dispatch,
                                       recovery, reversal; --lower-source /
                                       --emit-content-hash CLIs
microflows/runner/tests/unit/        ir_exec_test, ir_graph_test, parser_test
integration/coordinator-singular/    test.py — the 165-check E2E gate
singular/doc/singular-protocol.md    the participant-side protocol contract
```

### 12.9 LANDED — expression object/array construction

> **Roadmap item 1** — LANDED (2026-06-19). See `roadmap.md`.

Operation inputs (and any value expression) can now be **built from multiple
dynamic parts** — the natural shape for real workflows — with **no** weakening of
any runtime guarantee. This removes the former §12.7 "value construction"
workaround; authors wire inputs directly instead of pre-shaping the arguments
document.

**Surface (shipped).**
- Object literals with **expression-valued fields**:
  `{ customer: arg c.id, amount: arg order.amount }`.
- Array literals with **expression elements**:
  `[ arg a, result b, const 3 ]`.
- Nesting composes (a constructed object as a field value, etc.).
- A **fully-constant** literal folds to `{const: …}` — byte-identical to the
  former const-object shorthand, so existing `content_hash`es are unchanged (the
  parser folds; the IR re-canonicalizes the const regardless). Any dynamic
  field/element makes it an `EObject`/`EArray`.

**IR.** Two new `IrExpr` arms — `EObject(fields: Array<ExprField>)` and
`EArray(elems: Array<IrExpr>)` (`ExprField{key, value}`) — in `ir.drift`. They
carry only sub-expressions (never an operation), so a construction is a pure
value step. `graph_canonical` encodes object fields **sorted by key**
(key-order-insensitive) and array elements **in order**; canonicalization,
`validate_graph` (dominance recurses into children), the interpreter (`_eval`),
and `type_check_graph` (infers a `TObject`/`TArray`; `Imprecise`→`Unknown`→`Known`
precedence) all extend to the new arms. Config surface: `{"object": {k: <expr>}}`
/ `{"array": [<expr>]}` (strict).

**Proven invariants.**
- **Pure** — construction writes **no event**; resume **recomputes** it from
  durable args/results (event-count parity with a const-input op; trailing op
  reconciles GET-first, no second PUT). Integration C18 + unit.
- **Typed before dispatch** — a constructed value is type-checked against the
  operation's input contract (a field-type mismatch is rejected at lowering, no
  dispatch); folds into `content_hash`.
- **Determinism stays structural** — construction reads only durable
  args/results/locals; no clock/randomness.
- **Hash stability** — parser-lowered and hand-authored `{object}` graphs produce
  the identical `--emit-content-hash`; all-const literals fold to the same hash as
  before.

**Tests.** Unit `ir_exec_test` (EObject/EArray eval, parse/validate, canonical
key-order independence + array order-sensitivity, type pass/fail, dominance
recursion) and `parser_test` §16 (dynamic→graph, all-const→plan fold, object/
array/mixed/nested parity, dup-key + bad-keyword rejection); integration C18 (4
checks: hash parity + exec, type-mismatch rejected, no-event parity, resume
recompute). Full gate green on certified driftc 0.33.42 — integration **138/138**,
unit base + asan.

---

## 13. Operational admission / draining (first pass — roadmap item 2)

A coordinator must support a safe **update/drain/shutdown cycle**: stop taking new work, let in-flight
work converge, then reload or stop. This first pass establishes the **policy and shape** — not full
production machinery — at the boundary the future service will own.

**The runner is a one-shot CLI today** (claims one workflow, drives it, prints one outcome, exits);
there is no long-lived server or signal handler yet (the §4.1 service shell is unbuilt). So admission
is modeled as an **input to the drive boundary**, not in-process server state.

### 13.1 Admission states + the boundary

```text
accepting   normal (default)
draining    stop admitting NEW work; let existing work converge
stopped     same as draining for this slice (admit nothing new)
```

Admission is an **input to `_run`** — the boundary the future `drive_workflow(...) -> Outcome`
coordinator library exposes (roadmap item 2.5). The CLI/front-door determines the state and **passes
it in** (`_run(…, admission)`); the library never decides it. Source today: the `MICROFLOWS_ADMISSION`
environment variable (`"draining"` / `"stopped"`; unset/else = accepting), read once by the CLI.

### 13.2 Runner behavior

```text
FRESH submission (a brand-new workflow — a submission with NO durable pin):
  draining/stopped -> REFUSED before any create/claim/dispatch:
     {"workflow":"refused","reason":"draining"}   exit 10   (no workflow row, no dispatch)

EXISTING work (durable pin present — a resume OR a reassert):
  proceeds (so the drain converges). BUT a resume that would DEFER/retry returns, instead of
  scheduling new retry work:
     {"workflow":"pending_restart","reason":"draining"}   exit 11
  (the lease is released at `now`; the workflow stays in its durable state, resumable once admission
   returns to accepting). This is the "no new defers while draining" rule — applied uniformly at the
   `_defer` / `_defer_pending` / `_defer_dispatch` helpers.
```

A workflow that **can** finish during drain still completes/reverses normally — only would-be *new
defers* are converted. Reuse: the same gate serves **config reload** and **graceful shutdown**.

### 13.3 Front-door contract (spec only)

The future long-running service translates a `refused` (and `pending_restart`) admission outcome into
an **HTTP 503** to new callers / load balancers — **no `Retry-After` yet** (added when the retry
schedule is modeled). The service also owns reload/shutdown signal handling and calls the same
`_run`/`drive_workflow` boundary with admission as a parameter — so item 2.5's extraction is
mechanical, not a redesign.

### 13.4 Tests

Integration C19: a fresh submission is refused while **draining** (refused outcome, exit 10, no
workflow row, no dispatch) and likewise under **stopped** (`reason:"stopped"`); the same config accepts
normally when accepting (control); and an existing respond-pending workflow, resumed while draining,
converges to `pending_restart` (exit 11) instead of a new defer. Full gate green — integration
**142/142**.

### 13.5 Scope / deferred

`accepting`↔`draining` is driven by an external `MICROFLOWS_ADMISSION` signal; a durable admission
store, a health endpoint, `Retry-After`, and SIGHUP/SIGTERM handling arrive with the front-door
service (roadmap items 3 / 5). The legacy single-op submission path is out of scope (deprecated;
the planned/graph path is the supported submission surface).

### 13.6 The drive boundary returns a structured `Outcome` (roadmap item 2.5 — LANDED)

The drive functions no longer print JSON inline. A typed **`Outcome`** variant captures every
machine-readable status (`Completed` / `AlreadyTerminal` / `Reversed` / `ResolvedException` /
`TerminalState` / `Active` / `Pending` / `PendingRestart` / `Refused` / `Error` / `Failed` / `Aborted` /
`Deferred` / `DeferFailed` / `Blocked` / `ReverseAborted` / `FailAborted`). `_oc_render` is the **single**
source of the JSON, `_oc_exit` the single source of the exit code; the CLI adapter (`main`) renders
once via `_emit`. `_run` is now `throws -> Outcome` — the coordinator-library boundary the future
front-door SERVICE calls directly (rendering the same `Outcome` to HTTP) instead of shelling around the
CLI. The conversion was byte-compatible: identical JSON and exit codes, pinned by the integration suite
(**142/142**) across two verifiable passes (introduce `Outcome` + centralized render; then return it +
render at `main`). This is the seam item 3 builds the manifest-driven service on.

---

## 14. Manifest-driven ScriptRegistry (roadmap item 3a — LANDED)

App teams deploy **named, pinned, validated workflow revisions** instead of hand-lowered config files.
This realizes the §4.1 registry as a **one-shot-CLI library concept** (the long-running service is
item 3b).

### 14.1 The manifest

```json
{
  "deployment": { "...": "db, participants, operations (the shared routing)" },
  "scripts": [
    { "name": "checkout-v1", "version": "1.0.0", "path": "workflows/checkout.mf" }
  ]
}
```

One `deployment` block binds every script to the routing it needs to validate and run (per-script
routing is a later refinement — starting there is guessing). Each script is a NAMED `.mf` source path.

### 14.2 Load = compile + validate the entire declared set (fail-fast)

`mfrunner --manifest <file>` loads the manifest and, for **every** declared script, reads the
`.mf`, lowers it over the deployment (`parser.lower`), and builds+validates the revision
(`_registry_build`: graph parse + structural validation + type-check + compensation + content_hash).
**Startup fails** (`{"workflow":"aborted","reason":"invalid_manifest"}`, exit 2) if any script is
missing/unreadable/invalid or any name duplicates — before any claim/dispatch (the §4.1 contract). The
result is a `ScriptRegistry` keyed by **name → (version, content_hash, plan_length, runnable config)**.

### 14.3 Submission names a script; creation pins the resolved identity

A SUBMISSION supplies `--script <name>` (+ `--arguments`); the runner resolves the active declared
revision and **pins its immutable identity** at creation — **script name, plan version, content_hash,
plan_length** (`tb_mf_workflow_plan`). The manifest-lowered revision is byte-identical to a direct
`--lower-source` (same content_hash — script name/version are pin identity, not part of the hash). An
unknown `--script` is refused (`unknown_script`, exit 2) before any create/dispatch.

### 14.4 Resume drives strictly by the durable pin

A RESUME (`--manifest <file> --workflow-id …`, no `--script`) reads the durable pin and resolves the
script matching the pin's **(name, version)** — **never the manifest's current active version** (no
silent substitution). When the pinned revision IS in the manifest, the drive proceeds with that
script's config. When it is **NOT** (a rolled-forward manifest no longer declaring it), the runner does
**not** short-circuit: it drives the proven STORAGE-FIRST planned path with the deployment-only config,
so the durable pin governs state-sensitively — exactly as an absent pinned plan does on the `--config`
path:

```text
terminal workflow  -> terminal replay from durable state (registry-INDEPENDENT — the pinned
                      plan_length gives the final op seq; current routing/config is never consulted)
claimable workflow -> a DURABLE `revision_unavailable` defer (lease cleared, next_attempt set,
                      admission-aware via _defer_dispatch) — recoverable, never a substitution
no durable pin     -> not_found
```

The drive itself is unchanged: the manifest layer resolves the config and calls the same `_run_cfg` /
`_run_planned` boundary (which returns the structured `Outcome`, §13.6).

### 14.4.1 Script paths

A manifest script `path` is resolved **relative to the manifest file's directory** (so a manifest is
portable — not tied to the runner's cwd); an absolute path (leading `/`) is used as-is.

### 14.5 Scope / deferred

One deployment block; the manifest is loaded per-invocation (the one-shot CLI re-validates the set each
run — cheap and stateless). A **long-running service** that holds the registry, does **atomic/staged
reload** (SIGHUP/SIGUSR1, §4.1), owns the **admission gate** (§13), and serves submit/resume over HTTP
via `drive_workflow` is **item 3b** — now a thin wrapper around three landed pieces (registry +
admission + the `Outcome` boundary), not a redesign.

### 14.6 Tests

Integration C20: a named submission pins+runs with **content_hash parity** vs direct lowering; an
unknown `--script` is refused with no workflow row; a manifest with any invalid script fails at load
(`invalid_manifest`, no workflow row); and a respond-pending workflow submitted via the manifest
**resumes strictly by its durable pin** (no `--script`). Full gate green — integration **165/165**.

---

## 15. ScriptRegistry service shell (roadmap item 3b — LANDED)

A **second artifact** (`uflowsd`, entry `microflows.runner::service_main`) turns the
proven one-shot CLI core into a long-running `web.rest` front-door. It adds **no workflow semantics** —
it is a thin wrapper over the SAME drive boundary — but it is real service infrastructure.

### 15.1 Shared state + the host

`service_main` loads + validates the WHOLE manifest once (fail-fast, exactly as the CLI), builds ONE
host (its own internally-pooled DB connections via `mf.with_json`), and holds an `arc`-shared
`ServiceApp { host, registry: Mutex<Arc<ManifestSet>>, admission: AtomicInt, manifest_path }`. The host
is built once and shared across all requests — the enabling refactor was extracting **`_run_core(host,
cfg, …)`** from `_run_cfg` (the CLI builds a fresh host per invocation; the service reuses one). Per-
workflow leases/fencing in the storage layer keep concurrent drives safe; a request takes a cheap
snapshot of the registry `Arc` under the lock, releases it, then drives — so a concurrent reload never
disturbs an in-flight request.

### 15.2 Routes + Outcome → HTTP

Internal API: `POST /v1/workflows/{id}/submit?script=NAME` (body = instance arguments),
`POST /v1/workflows/{id}/resume` (driven strictly by the durable pin), `GET /healthz` (liveness),
`GET /readyz` (ready ⇔ accepting). Each workflow request calls the same `_drive_manifest_request` →
`_run_core` → **Outcome** the CLI uses. `_outcome_http_status` maps the Outcome arm to a semantic
status (200 terminal · 202 pending/deferred · **503 refused/pending_restart** (drain back-pressure, §13)
· 400 invalid/unknown-script/malformed-arguments · 404 not_found ·
409 conflict/leased/blocked · 500 internal); the **body is the EXACT Outcome JSON** (`_oc_render`,
unchanged), so a CLI consumer and an HTTP consumer read identical outcome documents.

### 15.3 Runtime admission + staged reload (signals)

Admission starts from `MICROFLOWS_ADMISSION` and is runtime-mutable. The main thread runs a signal
loop while `web.rest` serves in the background:

```text
SIGTERM  -> admission := draining  (fresh submissions -> Refused -> HTTP 503; /readyz -> 503)
            then graceful rest.shutdown (in-flight requests finish)  — the drain converges
SIGUSR1  -> staged reload: load+validate a NEW manifest into a standby ScriptRegistry; on success
            atomically SWAP the active Arc under the lock; on ANY failure keep serving the OLD one
SIGINT   -> immediate graceful shutdown
```

The admission gate is the SAME one the drive already applies (§13) — the service only chooses the
initial state and flips it on SIGTERM; the refusal/`pending_restart` behavior is unchanged.

### 15.4 Security boundary

**Internal API only.** Auth is roadmap item 5: the `/v1/workflows` route group is the seam where a
`web.rest` auth middleware / a request security context attaches later — no auth logic is built here,
and the drive still requires `auth_profile` to be null (§12.7). Deliberately not guessed now.

### 15.5 Tests

Integration C21 boots the service against the live stub and drives it over real HTTP: health/readiness;
a submission completes (`200`, durable reservation); resume replays the terminal result by pin
(`already_terminal`); an unknown script is `400`; a **malformed submit body** is a structured `400`
with **no workflow row** (never silently `{}`); a **SIGUSR1 reload** makes a freshly-declared script
runnable (was `400`, becomes `200` after the swap); and a service booted **draining** refuses fresh
submissions with **`503`** *and* converges an existing pending workflow resumed under drain as
**`pending_restart` → `503`** (the §13 back-pressure contract), reporting `/readyz` not-ready. Full gate
green — integration **165/165**.

---

## 16. Workflow composition — LANDED

A workflow step can call **another workflow** and await its terminal outcome, as an ordinary durable
step. Full design/history: `work/workflow-composition/DESIGN.md` (the original charter + slice plan)
and `work/workflow-composition/1c-design.md` (the compensation transition spec); current status is
tracked in `work/workflow-composition/PROGRESS.md`. This section summarizes the as-built shape.

### 16.1 Syntax, identity, and data flow

`let r = call child@1.0.0 { field: arg x }` (or as a bare statement, discarding the result) occupies
one forward step, exactly like a participant operation — same seq/settle discipline, same durable
checkpoint. `@1.0.0` is a semantic-version token resolved against the manifest's script registry at
build time (exact match → the child's pinned `content_hash`); a `call` to an unresolvable
name/version, or one that would create a call cycle or exceed `max_call_depth`
(`deployment.workflow_call.max_call_depth`, default in `runner.drift`), is rejected at build/dispatch
time — never silently accepted. The child's own workflow id is derived deterministically from
`(parent workflow_id, parent content_hash, call node id)` — domain-separated from a participant
operation id — so a resumed drive re-derives the identical child id and never creates a second one,
and two different parent *instances* of the same script (same `content_hash`, different
`workflow_id`) never collide on the same call node. The child's `return`
type is bound to the call's result binder; `result r.path` downstream reads it exactly like a
participant result.

### 16.2 Control flow — no block cascade

A child that is still forward/reversing/`blocked_resolution` is simply **non-terminal**: the parent
defers and stays `pending` (rendered `Outcome::Pending`, carrying the child's id so an operator can
see what it's waiting on) — it never adopts the child's `blocked_resolution` state itself. This holds
on both the forward side (`_run_forward`'s `NeedCall` await) and the reverse side (§16.4) — a stuck
descendant anywhere in the tree never blocks an ancestor; an operator resolves the *actual* stuck
workflow directly, and the next poll up the chain observes it terminal and proceeds. A child that
completes advances the parent with its typed return; a child that terminates *without* completing
(reversed / failed / resolved_exception) is a call rejection — the same as a definite participant
rejection — and begins the *parent's own* reversal.

### 16.3 Recovery — idempotent by construction

`call_submit` is called unconditionally on every pass, fresh or resumed (like `call_submit`'s
participant-op sibling `operation_request`) — a resumed drive re-enters the same step, `ir.advance`
deterministically re-evaluates the same input (a pure function of durable arguments/prior results),
and `call_submit` reconfirms agreement against the durably-stored identity rather than re-creating
anything. A crash between the child's completion and the parent's own settle simply resumes the same
await/settle sequence on the next drive. No new recovery machinery was needed for composition — it
reuses the same claim/lease/fencing/idempotent-replay discipline every workflow already has.

### 16.4 Compensation — reverse-child ("T1")

If the parent itself reverses, a call checkpoint compensates by asking the (already-completed) child
to compensate **itself**, recursively — the parent never enumerates or reaches into a child's own
checkpoints:

1. `checkpoint_reverse_child_reopen` ("T1") is called unconditionally on every reverse pass of that
   checkpoint (same idempotent-every-pass philosophy as `call_submit`). Fresh: reopens the child
   `completed(4) → reversing(2)` in one fenced parent+child transaction (dual event-time-skew check
   against both timelines), appending a `compensation_requested` event on the parent and a
   `compensation_requested_by_parent` event on the child. Replay: idempotent (`AlreadyReopened`),
   keyed on the child's *current* state, not a persisted dispatch flag — a true replay writes nothing.
2. The parent reads the child's current state via the SAME `call_inspect` the forward await already
   uses. Non-terminal → defer, no cascade (§16.2). Terminal → attempt settle.
3. `checkpoint_reverse_child_settle` independently re-verifies the child reached a genuinely
   compensated terminal (`reversed(5)` or `resolved_exception(6)` — settled identically, since the
   parent's control flow must never branch on which; the audit event records which as a passive
   correlation field only) before flipping the parent's own checkpoint. Any other state (still
   forward/reversing/blocked, or `failed`) is refused with a diagnostic outcome and no write — a
   settle can never be tricked into treating an uncompensated or corrupted child as done.
4. Once the child is settled, the parent's reversal continues exactly as a participant checkpoint
   does: descend to the next checkpoint, or reach the parent's own terminal `reversed(5)`.

Because a reversing child is just an ordinary reversing workflow, this is the **same mechanism,
applied recursively** through arbitrarily nested call chains — a chain A→B→C compensates as A reopens
B, B's own reverse loop reopens C, C compensates (its own participant checkpoint, if any, unwinds via
the unchanged pre-composition compensation path), and the settles cascade back up B→A. No
per-nesting-depth special case exists anywhere in this path.

### 16.5 Observability

`microflows-viz` (`microflows-viz/`, the successor to — and sole replacement for — the retired
`mfinspect` CLI) is the operator tool: its `serve` backend (read-only, viz_ro) exposes
`GET /api/workflow/<workflow_id>` for a full recursive JSON dump of a workflow's
call/event/checkpoint tree, `GET /api/workflows?script&since&until` to find candidates first, and the
derived tree/timeline/stuck views, with a browser live mode on top. Every T1/settle event carries
`child_workflow_id` (and, on the child's own reopen event, `parent_workflow_id` + the triggering
`operation_seq`), so a durable event, a service log line, and an inspect dump can all be joined by
the same identifiers.

### 16.6 Tests, MVP scope, and explicit exclusions

Full gate green — `microflows`'s unit/e2e suite (including host-wiring proofs in
`live_call_test.drift`), the SP-layer regression (`sp_call_test.py`, **131/131**), and the
runner-level integration suite (`call_integration_test.py`, **50/50** — including a nested A→B→C
acceptance case asserting the full chain's final states *and* that no level's audit trail references
a grandchild's identifiers). **MVP scope**: a single async workflow call, typed args/return, no block
cascade, reverse-child compensation, and workflow-tree inspection (now via `microflows-viz`).
**Explicitly deferred** (not
forgotten — tracked in `work/workflow-composition/PROGRESS.md`'s backlog): fan-out (parallel/multiple
concurrent calls from one parent), `on failed`/failure-as-data (surfacing a child's rejection as a
branchable value instead of always driving reversal), a stuck-child liveness budget, and a separate
compensating-workflow mode (`compensation <wf>@<version>` stays build-rejected, unchanged from the
original 1a slice).
