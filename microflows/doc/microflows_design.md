# Microflows Design

**Status:** Working design of record (supersedes `phase_drift_mile_design.md`)
**Audience:** implementation team
**Purpose:** define Microflows as a typed, durable **workflow coordinator** —
its responsibility boundary, preserved runtime decisions, the remote
participant protocol, and the milestone-1 plan.

> Renamed from **PhaseDrift** on 2026-06-07. The prior paper
> (`phase_drift_mile_design.md`) is preserved as the historical record; see
> §10 (History & rationale) for why the direction changed and what it cost.

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
  event_seq ordering, and the §24.4 time/command discipline.
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
  event_seq is causal order, no AUTO_INCREMENT) .......... prior §24.4
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
  200  terminal     { state: succeeded|failed, result|error }
  202  pending      { state: pending }
       deferred     { state: deferred, not_before: "..." }   (busy, due time)
  404  unknown      participant has NO record of this operation
  body state vocabulary: pending | succeeded | failed | indeterminate
```

`{operation}` is the static operation name; `{operation_id}` is the
Microflows-assigned stable identity (§5.2).

**404 during reconciliation** means the participant has **no record** of the
operation — it never received it (or never durably stored it). Microflows MAY
then **safely resubmit the identical request under the same ID**: idempotent
creation makes a genuine first execution and a re-creation indistinguishable.
(A 404 is therefore *not* a failure — it is a "not yet" that the dispatch gap
permits.)

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
event_seq remains the authoritative causal ordering; timestamps must remain
  chronological alongside it.
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
2a Schema: REMOVE record + journal stores ........... safe now (decoupled)
3  Lease acquisition + transactional fencing ........ DONE (reused as-is)
8  Minimum REST participant protocol + feasibility
     spike .......................................... NEXT — drives 2b
2b Schema: operation request/result + definition
     tables ......................................... SHAPED BY the spike
     (atomic request+continuation persist; input hash + schema version;
      normalized outcome incl. durable indeterminate)
4  Durable remote-operation dispatch + result
     persistence, with manual IR .................... after 2b
5  Crash recovery + idempotent resumption ........... concept preserved
6  Reversal + blocked_resolution
     (reverse = remote compensation dispatch) ....... concept preserved
9  Generic REST dispatcher .......................... after the spike proves
     the model
7  Parser + type checker ............................ after runtime proven
10 Script deployment: manifest-driven ScriptRegistry
     (compile-on-startup + staged SIGHUP reload) .... after 7; see §4.1
```

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
../drift-lang ............ parser, type checker, IR, diagnostics, interpreter
                           patterns (see doc/drift_lang_reuse.md)
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
