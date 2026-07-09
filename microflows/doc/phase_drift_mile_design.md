# PhaseDrift + MILE Reference Host Design

> **STATUS: SUPERSEDED — HISTORICAL RECORD (2026-06-07).**
> This paper described **PhaseDrift**, a workflow language whose runtime
> *owned* a transactional storage substrate (MILE Store): each Phase was one
> atomic local commit of business records + journal + checkpoint via
> `rw`/`ro`/`create`/`journal` guards over a generic business-record store.
>
> The project has been renamed **Microflows** and narrowed to a **durable
> workflow coordinator** that calls typed remote operations on participant
> services which own their own data and idempotency. The local-transaction
> Phase model, the MILE storage engine, the business-record store, and the
> `rw`/`ro`/`create`/`journal` guards are **withdrawn**.
>
> This document is preserved **unchanged below** as the rationale of record
> for what the local-transaction model offered and why it was set aside (see
> `microflows_design.md` §"History & rationale"). The **current design of
> record is [`microflows_design.md`](microflows_design.md)**. Many lifecycle
> and runtime decisions here are **preserved** by the new design — leases,
> fencing, durable continuations, checkpoints, reverse-order compensation,
> `blocked_resolution`, cancellation, recovery, `event_ts` chronological ordering, and the
> §24.4 time/command discipline — and are referenced from there rather than
> duplicated.

**Status:** Working design paper — **SUPERSEDED** (see banner above)
**Audience:** implementation team  
**Purpose:** describe PhaseDrift as a standalone lifecycle/workflow language, and MILE Store as the first host runtime/reference implementation.

---

## 1. Executive summary

PhaseDrift is a standalone typed lifecycle language/runtime for composing safe workflows from **Workflows**, **Phases**, **Guards**, and **Checkpoints**.

Its central goal is:

> Humans declare intent; the runtime unwinds actions.

Developers should not be relied upon to manually call cleanup, rollback, unlock, close, stop, release, or compensation logic. Large programs with nested scopes and long workflows are too easy to get wrong if humans must remember every cleanup path.

PhaseDrift provides the high-level lifecycle model. A host runtime provides the hard guarantees underneath. The first reference host is **MILE Store**, a durable record/document storage engine designed for atomic phase commits, checkpoint durability, recovery, replication, and auditable workflows.

PhaseDrift itself is not MILE-specific. MILE is the first host/runtime target.

---

## 2. Terminology

### Workflow

A **Workflow** is an ordered scope of Phases.

Example:

```phasedrift
workflow refund_parent(
    refund_id: Id,
    parent: Id,
    amount_cents: Int
) {
    val staged = stage_refund(refund_id, parent, amount_cents)
    val submitted = submit_refund_to_processor(staged)
    val settled = settle_refund(submitted)

    close_refund(settled)
}
```

A Workflow records each successful Phase as a Checkpoint. If the Workflow fails, active Checkpoints are reversed in reverse order.

---

### Phase

A **Phase** is the unit of work invoked by a Workflow.

A Phase is not just a normal function. It is a managed lifecycle unit.

A Phase has:

```text
apply
reverse
```

`apply` is the normal path. If `apply` completes successfully and commits, it produces a Checkpoint.

`reverse` is used later if the surrounding Workflow backs out after this Phase has already committed.

Example:

```phasedrift
phase stage_refund(
    refund_id: Id,
    parent: Id,
    amount_cents: Int
) -> RefundStage {

    apply {
        ...
        return RefundStage { ... }
    }

    reverse(cp: RefundStage) {
        ...
    }
}
```

---

### Guard

A **Guard** protects unfinished work inside a currently running Phase.

A Guard is a scoped runtime value returned by a host-provided function.

Examples in the MILE host:

```phasedrift
val account = rw(accounts/{account_id}/money)
val policy = ro(refund_policies/current)
val display = peek(parents/{parent}/profile)
val refund = create(refunds/{refund_id}, Refund { ... })
```

Examples in other possible hosts:

```phasedrift
val tmp = temp_file("report")
val pump = run_pump("pump-1")
val lease = claim_job("projection/CUSD200")
val slot = claim_deploy_slot("prod")
```

A Guard answers:

> What happens if the current Phase scope exits before or after success?

For MILE, a `rw(...)` Guard is conceptually similar to `SELECT ... FOR UPDATE`, except it is scoped, staged, and automatically committed or rolled back by the Phase runtime.

---

### Checkpoint

A **Checkpoint** is the durable result of a successfully committed Phase.

A Checkpoint contains enough information to:

```text
resume workflow execution
reverse the Phase if the Workflow later fails
prove/audit what happened
```

The Checkpoint stack records completed Phases and their reversal order. It is
not, by itself, the Workflow's execution position. The runtime also persists a
durable continuation containing the next workflow position and any typed local
values needed after the Phase boundary.

A Checkpoint may be visible:

```phasedrift
val staged = stage_refund(...)
```

or hidden:

```phasedrift
stage_refund(...)
```

Even when hidden, the runtime still records the Checkpoint.

Assignment only exposes the Checkpoint to later code. It does not determine whether the Checkpoint exists.

---

## 3. Core teaching model

The clearest model is:

> Guards roll back unfinished work. Checkpoints reverse finished work.

Or:

```text
failure before Phase commit
  -> Guards roll back/discard unfinished work

failure after Phase commit
  -> Checkpoints reverse committed work
```

This distinction is critical.

---

## 4. Phase lifecycle

A Phase invocation has one atomic commit boundary.

Conceptually:

```text
invoke phase
  run apply

if apply exits with error:
  abort Guards
  discard staged effects
  no Checkpoint exists
  Phase did not happen

if apply exits normally:
  commit all staged effects atomically
  persist Checkpoint atomically with effects
  persist the Phase result and next Workflow continuation atomically
  advance Workflow state
```

A Phase commit must include:

```text
data writes
created records
journal/audit entries
workflow progress
checkpoint stack update
returned Checkpoint
next Workflow continuation
```

All of these become visible together or none of them become visible.

There must never be:

```text
money moved but checkpoint missing
journal emitted but record not updated
workflow advanced but data missing
record created but phase not marked committed
```

### 4.1 Phase result and continuation durability

A committed Phase must make its typed result durably available to the
Workflow. If the result is bound to a variable, that variable refers to the
Checkpoint's typed payload:

```phasedrift
val lines = load_invoice(invoice_id)
```

Here, `lines` is available after recovery because the `load_invoice` result is
published in the same atomic commit as the Phase effects and Checkpoint.

The atomic unit is:

```text
Phase effects
+ Checkpoint and its typed result
+ updated Checkpoint stack
+ next Workflow continuation
= one commit
```

The continuation contains the next executable workflow position and any
ordinary typed locals that must survive that boundary. Such ordinary persisted
locals are durable locals or continuation values; they are not Checkpoints.

Large Phase results may be represented by an immutable snapshot reference,
revision, and content hash rather than embedding the complete value. Recovery
must not substitute a fresh read of mutable current data.

If this commit fails before publication:

```text
the Phase did not commit
no Checkpoint or result exists for it
its Guards abort
earlier Checkpoints remain active
the Phase may be retried with the same stable invocation ID
```

Earlier Checkpoints are reversed only if the Workflow itself is subsequently
declared failed. A failed attempt to persist the current Phase does not by
itself reverse already committed Phases.

---

## 5. Guard lifecycle

Internally, a Guard is a scoped object/value with lifecycle behavior.

Possible implementation vocabulary:

```text
acquire
success
error
release
```

or:

```text
enter
success
error
exit
```

However, users should not usually think in these terms. User-facing docs should say:

> A Guard is active while the scope is running. If the scope succeeds, the Guard contributes to the Phase commit. If the scope fails, the Guard cleans up or discards unfinished work.

For MILE `rw(path)`:

```text
acquire:
  lock/read the record
  create transaction-local working copy

mutation:
  modify the working copy only

success:
  contribute dirty working copy to the Phase commit plan

error:
  discard working copy

release:
  release lock/authority
```

Important:

> A Guard does not independently commit. Guards contribute staged effects to the Phase commit coordinator. The Phase commit persists everything atomically.

Example:

```phasedrift
val account = rw(accounts/{account_id}/money)
account.wallet_cents -= amount_cents
```

The assignment mutates a transaction-local view. The updated record is persisted only during the Phase's atomic commit after `apply` returns successfully.

---

## 6. Checkpoint lifecycle

A Checkpoint exists only after a Phase successfully commits.

Workflow success:

```text
active checkpoints are dismissed/closed
```

Workflow failure:

```text
active checkpoints are reversed in reverse order
```

Example:

```phasedrift
workflow transfer_A_to_B(T123: Id, A: Id, B: Id, amount_cents: Int) {
    val hold = reserve_source_funds(T123, A, amount_cents)
    val applied = apply_destination_funds(B, hold)
    mark_transfer_settled(T123)
}
```

If `mark_transfer_settled` fails before commit:

```text
mark_transfer_settled Guards roll back automatically
apply_destination_funds Checkpoint reverses
reserve_source_funds Checkpoint reverses
```

---

## 7. `apply` and `reverse`

PhaseDrift uses the working pair:

```text
apply / reverse
```

`apply` creates a Checkpoint.

`reverse` consumes a Checkpoint if the Workflow backs out.

`reverse` does not necessarily mean restoring old bytes. It means applying the Phase's opposite/resolution transition for that Checkpoint.

In different domains, reverse may mean:

```text
restore prior state
post accounting reversal
move value to an exception drawer
release a reservation
open manual review
mark unresolved external state
```

The important rule is:

> `reverse` must leave the system in a valid, explicit, auditable state.

### 7.1 Reverse failure and resolution

Reverse failure has first-class Workflow state and recovery semantics.

A Workflow has lifecycle states:

```text
forward
reversing
blocked_resolution
completed
reversed
```

"Running" is not a lifecycle state: it means a claimable state currently has
a valid executor lease (§24). Retry and timer waits are likewise not states;
they are due-time scheduling on the workflow instance.

Each active Checkpoint has reversal state:

```text
active
reversing
reversed
resolution_required
resolved
```

A reverse invocation is itself an atomic, guarded, idempotent operation:

```text
reverse effects
+ journal entries
+ Checkpoint status update
+ reversal continuation
= one commit
```

If reverse fails before commit, none of its effects are published and the
Checkpoint remains active.

**Failure policy.** Transient failures are retried:

```text
lock conflict
ro validation conflict
temporary storage failure
retryable host failure
```

Business or invariant failures are not blindly retried:

```text
assertion failure
unexpected record state
authorization failure
missing required record
irreconcilable external state
```

For a nonretryable failure or exhausted retries:

```text
1. Mark the Checkpoint resolution_required.
2. Mark the Workflow blocked_resolution.
3. Persist the failure, observed state, attempt IDs, and diagnostics.
4. Stop automatic reversal.
```

Stopping is important. Reversal order expresses dependencies. If Checkpoint C
cannot reverse, automatically reversing B and A may violate assumptions or
accounting invariants.

The resulting state is not "successfully reversed," but it remains explicit
and auditable:

```text
C: resolution_required
B: active
A: active
Workflow: blocked_resolution
```

Checkpoints after C may already have been successfully reversed before C
failed.

**Resolution operations.** The runtime supports explicit, authorized actions:

```text
retry
resolve using a declared resolution Phase
mark externally resolved with evidence
abandon/accept exception
```

A resolution action must atomically record:

```text
operator or service identity
reason
evidence/reference
resulting system state
Checkpoint disposition
next reversal position
```

After resolution, the Workflow may continue reversing earlier Checkpoints or
terminate in a resolved-exception state.

"Skip this Checkpoint" is not an ordinary operation. It requires an explicit
audited exception disposition because earlier reversals may depend on it.

**Programming guidance.** Expected alternate states should usually be handled
within `reverse`, not asserted away:

```phasedrift
reverse(cp: RefundStage) {
    match current_state {
        staged => restore_parent_balance(cp)
        operator_moved => route_to_manual_exception(cp)
        settled => record_irreversible_resolution(cp)
    }
}
```

Assertions remain appropriate for states that indicate a defect or violated
contract. Their failure blocks reversal.

Compensation-strength metadata helps predict possible outcomes, but does not
replace these runtime semantics:

> A failed reverse attempt publishes nothing, leaves its Checkpoint active,
> and blocks further automatic unwind unless a declared policy safely
> resolves it.

---

## 8. Hidden Checkpoints

A Phase does not need to be assigned to a variable.

This:

```phasedrift
workflow refund_parent(...) {
    stage_refund(...)
    submit_refund(...)
    settle_refund(...)
}
```

is conceptually:

```phasedrift
workflow refund_parent(...) {
    hidden _cp1 = stage_refund(...)
    hidden _cp2 = submit_refund(...)
    hidden _cp3 = settle_refund(...)
}
```

If later reversal is needed, hidden Checkpoints are still reversed.

This is critical because developers should not have to assign variables merely to get safety.

---

## 9. Types and schemas

PhaseDrift is typed. It should not be loose JSON.

Data may be document-shaped, but every stored field and value should have a schema type.

Built-in types should be capitalized:

```text
Bool
Int
Decimal
String
Bytes
Date
Time
DateTime
Duration
Id
Uuid
Array<T>
Map<K,V>
```

For financial systems, avoid floating point.

Prefer:

```phasedrift
amount_cents: Int
currency: Currency
```

or a fixed-scale `Money` type if explicitly designed.

Example schemas:

```phasedrift
schema Refund {
    refund_id: Id
    parent: Id
    amount_cents: Int
    state: RefundState
    drawer: Drawer
    processor_ref: String?
    created_at: DateTime
}

enum RefundState {
    staged
    submission_pending
    accepted_by_processor
    settlement_pending
    settled
    external_exception
    settlement_exception
    cancelled_before_submission
}

enum Drawer {
    parent_refundable_liability
    refund_staging
    processor_refund_pending
    settlement_clearing
    external_refund_exception
    settlement_exception
}
```

---

## 10. MILE Store as first host runtime

MILE Store is the first reference implementation/host for PhaseDrift.

MILE provides durable document/record storage and host functions such as:

```phasedrift
rw(path)
ro(path)
peek(path)
create(path, value)
journal { ... }
```

MILE maps these to storage primitives:

```text
rw(path)
  mutable record Guard
  similar to SELECT FOR UPDATE
  acquire write authority
  use transaction-local copy
  commit/rollback via Phase lifecycle

ro(path)
  stable read Guard
  track version/revision
  validate at Phase commit

peek(path)
  advisory read
  no commit dependency
  may be cached/stale depending on host policy

create(path, value)
  new-record Guard with initialized creation
  reserve key/assert nonexistence
  returns a writable Guard over the staged record
  create only if Phase commits

journal { ... }
  staged journal/audit contribution
  committed atomically with Phase
```

### 10.1 Initialized creation

`create` takes the new record's initial value:

```phasedrift
val refund = create(
    refunds/{refund_id},
    Refund {
        refund_id: refund_id
        parent: parent
        amount_cents: amount_cents
        state: RefundState.staged
        drawer: Drawer.refund_staging
    }
)
```

`create` returns a writable Guard over the staged record. The binding
(`refund`) is immutable, while fields of the transaction-local record may be
mutated:

```phasedrift
refund.state = RefundState.cancelled_before_submission
```

The created record remains invisible until the Phase commits. If
initialization or the Phase fails, no record is created.

Assignment of a record literal to the binding:

```phasedrift
refund = Refund { ... }
```

is not used to mean "initialize the Guard." It would appear to rebind an
immutable variable and obscure whether assignment targets a binding, a record,
or a host capability. Variable binding and managed-record mutation must remain
distinct. Initialized `create(path, value)` gives the operation explicit
syntax and lets the type checker verify that the path schema and the supplied
record type agree.

---

## 11. MILE atomic Phase commit

MILE must treat each Phase commit as one atomic batch.

A Phase can have many staged effects:

```text
record updates
record creates
record deletes
journal entries
workflow state update
checkpoint stack update
checkpoint object
index updates
```

But there is only one commit boundary.

Implementation should use an append/write-then-publish model.

Conceptually:

```text
1. Write all new objects/data somewhere not yet visible.
2. Make them durable.
3. Publish by advancing one durable root/head pointer or revision.
```

Readers see the current published root.

Example:

```text
current_root -> revision 100
```

A Phase produces revision 101:

```text
new account record version
new refund record
new journal entry
new workflow state
new checkpoint
new commit manifest
```

These are written but not visible while `current_root` still points to 100.

Commit becomes visible only when:

```text
current_root -> revision 101
```

If disk fills before publish:

```text
current_root still points to revision 100
partial revision 101 is unreachable/unpublished
Phase did not commit
```

If crash happens before pointer advance:

```text
revision 100 remains visible
```

If crash happens after pointer advance:

```text
revision 101 is visible
recovery completes cleanup/derived work as needed
```

There must be no visible partial Phase.

---

## 12. Commit object shape

A MILE commit object may contain:

```text
commit_id / revision
parent_commit
workflow_id
phase_id
phase_attempt_id
apply_or_reverse marker
writes
created records
deleted records
journal entries
checkpoint produced
workflow state update
read_versions / validation set
hash / checksum
```

Example:

```text
Commit 101:
  parent: 100
  workflow_id: refund/R123
  phase_id: stage_refund
  kind: apply
  writes:
    parents/P1/money -> object_hash_A
    refunds/R123 -> object_hash_B
    journal/J991 -> object_hash_C
    workflow/R123 -> object_hash_D
  checkpoint:
    RefundStage { ... }
  read_versions:
    refund_policies/current: rev 44
  hash:
    H(parent, writes, checkpoint, read_versions)
```

The Checkpoint is not an afterthought. It is part of the same commit as the data changes.

---

## 13. Crash recovery requirements

After crash, MILE must be able to answer:

```text
Did this Phase commit?
What Checkpoint was produced?
What Workflow phase is next?
Which Checkpoints are active?
Is Workflow running, completed, reversing, or blocked?
```

Recovery must produce one of two outcomes for each Phase attempt:

```text
not committed
  no effects visible
  no checkpoint active

committed
  all effects visible
  checkpoint active or dismissed/reversed according to workflow state
```

No partial state is allowed.

---

## 14. Idempotency

Phase invocation and reverse invocation must be idempotent under retry.

The runtime should use stable identifiers:

```text
workflow_id
phase_id
phase_attempt_id
checkpoint_id
reverse_attempt_id
request_id
```

If a worker crashes or times out, another worker can retry or resume safely.

If an attempt already committed, retry should discover the existing commit/checkpoint rather than applying again.

### 14.1 Retryable apply and `ro` validation conflicts

A failed `ro` validation aborts the uncommitted attempt and transparently
retries the entire `apply`, subject to a bounded retry policy.

This rests on an explicit language guarantee:

> Phase `apply` is retryable. Before commit, it may affect the outside world
> only through transactional Guards whose effects can be discarded.

On conflict:

```text
1. Commit validation detects a changed ro revision.
2. No Phase effects are published.
3. All Guards abort and release in reverse order.
4. The runtime retries apply with fresh reads.
5. The logical Phase invocation retains the same stable invocation ID.
```

Each execution gets a distinct attempt number for diagnostics:

```text
phase_invocation_id: stable across retries
execution_attempt: increments per retry
```

Retries apply only to transient failures:

```text
ro validation conflicts
lock contention or deadlock victim selection
retryable storage/replication failures
```

These are not automatically retried:

```text
failed assertions
schema/type violations
authorization failures
declared business errors
nonretryable host errors
```

Retries must be bounded by attempts, elapsed time, or a Workflow policy.
Exhaustion returns a conflict/failure to the Workflow rather than spinning
indefinitely.

The language must prohibit direct effects inside `apply`, including:

```text
network calls
filesystem writes
process execution
unmanaged clock/random reads
logging that carries business or audit semantics
mutation of shared host memory
```

Such operations must use host capabilities with correct Guard semantics or be
modeled as separate externally idempotent Phases. Diagnostic tracing may occur
per attempt, but it must be clearly distinguished from committed audit records
and may contain duplicate attempts.

One important consequence: values such as time and randomness must remain
stable for an invocation if they influence committed behavior. Either the
runtime supplies deterministic invocation-scoped values, or a host Guard
stages and commits them. A retry must not silently become a logically
different operation. (Exact pinning semantics are an open point; see open
questions.)

Policy summary:

> `ro` uses optimistic validation. A conflict aborts the complete Phase
> attempt without visible effects. The runtime may transparently retry
> `apply` because Phase code cannot perform unmanaged side effects.
> Retryable conflicts, retry limits, and attempt identity are part of the
> host/runtime contract.

---

## 15. Accounting drawer model

For accounting/financial domains, use the drawer model:

> Money is never unaccounted for. It is always in exactly one known drawer.

A Phase moves value atomically from one drawer to another.

Example refund lifecycle:

```text
parent_refundable_liability
  -> refund_staging
  -> processor_refund_pending
  -> settlement_clearing
  -> closed_against_bank
```

Failure/exception paths:

```text
refund_staging
  -> parent_refundable_liability

processor_refund_pending
  -> external_refund_exception

settlement_clearing
  -> settlement_exception
```

Each arrow should be one atomic Phase:

```text
Dr old drawer
Cr new drawer
update phase state
emit journal/audit
produce checkpoint
```

The system may be incomplete, delayed, or failed. It must never be unbalanced.

Drawers are also the accounting form of inter-Phase reservation:

> Drawers are the inter-Phase reservation mechanism: value is parked in an
> explicit state where it remains accounted for and cannot also be spent from
> its prior drawer.

Moving value into `refund_staging` is the durable reservation between Phases.
It is not literally a database lock: later Phases still use `rw` to modify it
safely, but competing workflows observe that the value is no longer available
in its original drawer.

---

## 16. Leaf Phase rule

A Phase should be as small as possible, but not smaller than the invariant it protects.

For money, a leaf Phase is usually:

```text
move value from drawer A to drawer B
update state
emit journal/audit
produce checkpoint
```

This should not be split into separate committed Phases:

```text
1. update state
2. move money
3. emit journal
4. record checkpoint
```

because that would allow invalid intermediate states:

```text
state says processor_pending but money is still in refund_staging
money moved but journal missing
journal says move happened but record disagrees
checkpoint exists but data did not commit
```

Rule:

> If splitting a Phase creates a state where value is unaccounted for, double-counted, or state/audit disagree, the Phase is already at leaf size.

---

## 17. External systems

External systems cannot be treated like local atomic writes.

A payment processor, bank, shipping provider, email service, or device may accept a request and later fail, delay, reverse, or become ambiguous.

Therefore external workflows must be mirrored internally as phase machines.

Do not model a refund as:

```text
refund_success = true
```

Model the lifecycle:

```text
staged
submission_pending
accepted_by_processor
settlement_pending
settled
failed_after_acceptance
ambiguous
manual_resolution_required
```

Each externally meaningful boundary needs an internal state/drawer if compensation behavior changes there.

Rule:

> Never compress an external workflow into fewer internal phases than the external party can fail between.

External calls should generally be modeled as:

```text
phase 1: create durable local intent
phase 2: submit idempotent external request
phase 3: observe/query external state
phase 4: settle, clear, or move to exception drawer
```

If a processor says “OK, I am doing it,” that is not the same as settled. Internally, value should move to something like:

```text
processor_refund_pending
```

not disappear from accounting.

---

## 18. Example: refund flow in MILE/PhaseDrift

```phasedrift
workflow refund_parent(
    refund_id: Id,
    parent: Id,
    amount_cents: Int
) {
    val staged = stage_refund(refund_id, parent, amount_cents)
    val submitted = submit_refund_to_processor(staged)
    val accepted = mark_processor_accepted(submitted)
    val settled = reconcile_processor_settlement(accepted)

    close_refund(settled)
}
```

### stage_refund

```phasedrift
phase stage_refund(
    refund_id: Id,
    parent: Id,
    amount_cents: Int
) -> RefundStage {

    apply {
        val parent_money = rw(parents/{parent}/money)
        val policy = ro(refund_policies/current)

        assert amount_cents <= policy.max_auto_refund_cents
        assert parent_money.refundable_balance_cents >= amount_cents

        parent_money.refundable_balance_cents -= amount_cents

        val refund = create(
            refunds/{refund_id},
            Refund {
                refund_id: refund_id
                parent: parent
                amount_cents: amount_cents
                state: RefundState.staged
                drawer: Drawer.refund_staging
            }
        )

        journal {
            dr: "parent_refundable_liability/" + parent
            cr: "refund_staging/" + refund_id
            amount_cents: amount_cents
        }

        return RefundStage {
            refund_id: refund_id
            parent: parent
            amount_cents: amount_cents
        }
    }

    reverse(cp: RefundStage) {
        val parent_money = rw(parents/{cp.parent}/money)
        val refund = rw(refunds/{cp.refund_id})

        assert refund.state == RefundState.staged
        assert refund.drawer == Drawer.refund_staging

        parent_money.refundable_balance_cents += cp.amount_cents

        refund.state = RefundState.cancelled_before_submission
        refund.drawer = Drawer.parent_refundable_liability

        journal {
            dr: "refund_staging/" + cp.refund_id
            cr: "parent_refundable_liability/" + cp.parent
            amount_cents: cp.amount_cents
        }
    }
}
```

This Phase moves money between drawers atomically and produces a Checkpoint.

If `apply` fails before commit, Guards roll back.

If it commits and a later Phase fails, `reverse` moves the value back or to the appropriate drawer.

---

## 19. Host runtime contract

A PhaseDrift host must provide these guarantees.

### 19.1 Guard support

The host must support scoped Guard values with success/error behavior.

The language/runtime must unwind Guards automatically in reverse order.

### 19.2 Phase atomicity

A Phase must have one atomic commit boundary.

```text
apply succeeds -> all staged effects + checkpoint commit together
apply fails    -> all staged effects are discarded
```

### 19.3 Checkpoint durability

A committed Phase must durably publish its Checkpoint together with its effects.

No effects without Checkpoint. No Checkpoint without effects.

### 19.4 Workflow checkpoint stack

The host must preserve active Checkpoints and reverse them in reverse order if the Workflow backs out.

### 19.5 Crash recovery

The host must recover to either the previous published state or the full committed state.

No partial Phase visibility.

### 19.6 Idempotent retry

Phase apply/reverse must be safe under retry using stable IDs.

### 19.7 External side-effect discipline

If an effect cannot be atomically committed by the host, it must be modeled through staged/idempotent Phases rather than hidden inside a local atomic Phase.

### 19.8 Lock scoping and concurrency

All transactional locks are scoped to one Phase attempt and released on
commit or abort. A Checkpoint must never retain an in-memory lock, lease,
transaction, or open Guard between Phases.

Between Phases, exclusivity must be represented as durable domain state:

```text
reservation record
ownership field
lease with expiry and fencing token
workflow state
value held in a drawer
```

**Within a Phase.** Multiple `rw` acquisitions can deadlock:

```text
Workflow A: rw(X), then rw(Y)
Workflow B: rw(Y), then rw(X)
```

Policy:

```text
1. Encourage canonical resource ordering.
2. Detect deadlocks rather than relying only on timeouts.
3. Select one attempt as the victim.
4. Abort all its Guards and staged effects.
5. Retry its complete apply with backoff and jitter.
6. Preserve the logical Phase invocation ID; increment the attempt number.
```

Timeouts remain a fallback for unavailable nodes or failed lock holders, but
are not the primary deadlock detector.

Where the resource set is known before acquisition, the host should support
ordered bulk acquisition:

```phasedrift
val accounts = rw_all(account_paths)
```

`rw_all` canonicalizes paths and acquires them as one operation, rather than
expecting every program to manually sort resources. For dynamically discovered
resources, deadlock detection and retry remain necessary.

Runtime limits should cap:

```text
locks per Phase
lock wait duration
Phase execution duration
retry count
```

**Isolation at commit:**

```text
rw records must still be owned by the attempt and pass write-conflict checks
ro revisions must still match
create nonexistence claims must remain valid
all locks are released after atomic publication or abort
```

No Phase may wait indefinitely.

**Between Phases.** A Workflow requiring durable ownership must commit it
explicitly:

```phasedrift
phase reserve_inventory(...) -> InventoryReservation {
    apply {
        val inventory = rw(...)

        inventory.available -= quantity

        val reservation = create(..., Reservation {
            owner_workflow: workflow.id
            state: ReservationState.held
        })
    }

    reverse(cp: InventoryReservation) {
        ...
    }
}
```

The reservation survives because it is committed data represented by the
Checkpoint's effects, not because a lock remains held.

For time-limited ownership, use durable leases with fencing tokens. Expiry
alone is insufficient because a delayed former owner might still act after
another owner acquires the lease.

Principle:

> Guards provide concurrency control only within a Phase attempt. Checkpoints
> hold no runtime resources. Any protection or reservation needed between
> Phases must be represented as durable, auditable domain state.

---

## 20. Layering

PhaseDrift is a separate language/runtime. Drift may be used to implement the lower layers.

```text
┌────────────────────────────────────────────┐
│ PhaseDrift programs                         │
│ workflows, phases, guards, checkpoints      │
├────────────────────────────────────────────┤
│ PhaseDrift language/runtime                 │
│ parser, type checker, planner, executor     │
├────────────────────────────────────────────┤
│ Host Runtime contract                       │
│ guard lifecycle, phase atomicity, recovery  │
│ checkpoint stack, idempotency               │
├────────────────────────────────────────────┤
│ MILE Store reference host                   │
│ durable records, atomic publish, journals   │
│ replication, workflow state, checkpoints    │
├────────────────────────────────────────────┤
│ Drift implementation layer                  │
│ storage engine, WAL/COW, network, TLS,      │
│ scheduler, physical I/O                     │
├────────────────────────────────────────────┤
│ OS / disk / network                         │
└────────────────────────────────────────────┘
```

PhaseDrift defines lifecycle semantics.

MILE fulfills the first concrete host contract.

Drift can implement the machinery beneath MILE and the PhaseDrift runtime.

---

## 21. Design principles

### 21.1 No manual cleanup in ordinary code

If something must be cleaned up, released, stopped, rolled back, or reversed, it must be represented as a Guard or Checkpoint.

Bad default API:

```phasedrift
open_valve("v1")
start_pump("p1")
...
stop_pump("p1")
close_valve("v1")
```

Better:

```phasedrift
{
    val valve = open_valve("v1")
    val pump = run_pump("p1")

    wait_until_level(tank, 80)
}
```

The active condition owns its exit behavior.

### 21.2 Runtime owns unwind order

Guards unwind in reverse order inside a Phase.

Checkpoints reverse in reverse order inside a Workflow.

### 21.3 Visible variables are optional

A Checkpoint may be visible or hidden. Safety must not depend on assignment.

### 21.4 Reverse is not necessarily exact undo

`reverse` means applying a valid opposite/resolution transition for a committed Checkpoint.

### 21.5 Hosts must be honest

PhaseDrift cannot make unsafe host operations safe. Host primitives must fulfill lifecycle contracts. External/physical effects must be staged and mirrored as Phases.

---

## 22. Compilation and deployment model

PhaseDrift programs are compiled and executed as:

```text
PhaseDrift source
  -> parse/type-check/bind/verify
  -> versioned portable IR
  -> runtime interpreter
```

Scripts remain embedded and hot-deployable, like stored procedures. Deploying
a script produces an immutable script revision containing its verified IR.

Running Workflow instances stay pinned to the immutable script revision they
started under, unless explicitly migrated. The durable continuation (§4.1)
records execution position in terms of the pinned revision's IR, and reversal
of a Checkpoint uses the pinned revision's `reverse` code.

Migration of active instances is an explicit, separately governed operation
(see open questions). For milestone 1, active Workflow instances are not
migrated: they finish or reverse using their pinned revision.

---

## 23. Milestone 1: MariaDB reference host

The first implementation uses MariaDB/InnoDB as the reference host backend.
The PhaseDrift parser and runtime are written in Drift, using the
production-ready Drift MariaDB client library.

Each Phase maps to one SQL transaction containing:

```text
staged record changes
journal entries
Checkpoint and its typed result
Checkpoint-stack update
next Workflow continuation
```

Host function mapping:

```text
rw      -> locking reads (SELECT ... FOR UPDATE)
ro      -> revision validation at commit
create  -> uniqueness-constrained insert
```

### 23.1 Business-record storage (MVP decision)

> One PhaseDrift business record maps to one independently addressable
> MariaDB row. The complete typed PD value is serialized into one opaque
> JSON column and replaced atomically as a whole.

For milestone 1:

```text
a generic business-record table keyed by canonical record path:

  tb_pd_record (
      record_path     VARBINARY(...) PRIMARY KEY,
      type_id         VARBINARY(...) NOT NULL,
      schema_version  BIGINT NOT NULL,
      revision        BIGINT NOT NULL,
      value_json      JSON NOT NULL
  )

rw   performs a locking read, decodes and validates the complete PD
     value, and returns a transaction-local mutable Guard; field
     assignments modify only the in-memory PD value

at Phase commit, each dirty record is validated, canonically
     serialized, and replaced with ONE parameterized UPDATE

ro   reads the complete value and records its revision for commit
     validation

create(path, value) inserts the complete serialized value, protected
     by path uniqueness
```

Explicitly out of scope for record access: per-field SQL generation, stored
procedures, ORMs, and logical-to-relational binding layers. MariaDB treats
the value as opaque storage; **PD schemas remain authoritative** for its
structure and meaning.

Monetary values remain typed integers / fixed-scale values and must never
become floating-point JSON numbers.

Revisions are explicit and runtime-supplied — never auto-increment behavior.
IDs, revisions, timestamps, and other nondeterministic values are supplied
explicitly by the runtime and remain stable across retries (§24.4).

Journals, workflow events, invocations, checkpoints, continuations, and
leases remain dedicated runtime tables and commit **in the same transaction**
as changed business records.

Correctness is the priority: full-record replacement intentionally defers
indexing, projections, partial updates, table-per-type layouts, and legacy
relational adapters until the workflow model is proven and measured.

This milestone validates PhaseDrift's language and lifecycle semantics,
including retries, reversal, blocked resolution, concurrency, and crash
recovery. It does not claim to validate MILE's eventual native
write-then-publish storage architecture. MariaDB remains behind the host
contract so a native MILEHost can replace the initial MariaDBHost later
without exposing SQL semantics in PhaseDrift.

---

## 24. Workflow execution and lease model

Workflow instances are executed by N stateless executor workers. Workers are
identical; there is no leader. In milestone 1, MariaDB is also the
coordination substrate — no separate queue infrastructure.

### 24.1 Lifecycle state vs. lease ownership

Lifecycle state and lease ownership are separate. The lifecycle states are
those of §7.1:

```text
Workflow state        Claimable   Condition
--------------------  ----------  --------------------------------
forward               yes         due and unleased/expired
reversing             yes         due and unleased/expired
blocked_resolution    no          requires authorized resolution
completed             no          terminal success
reversed              no          terminal unwind
```

`running`, `retry waiting`, and `timer waiting` are not lifecycle states:

```text
"running" means a claimable state currently has a valid lease
retry and timer waits use next_attempt_at
recovery means the lease expired, not a special state transition
```

The claimable predicate:

```sql
state IN (forward, reversing)
AND next_attempt_at <= database_now
AND (lease_owner IS NULL OR lease_expires_at < database_now)
```

`database_now` is an explicit parameter the runtime sources from the database
clock — SQL never generates time itself (§24.4).

A resolution operation (§7.1) transitions `blocked_resolution -> reversing`,
sets `next_attempt_at` to now, and records its audit information.

### 24.2 Claim implementation

A claim is a short MariaDB transaction:

```sql
SELECT workflow_id
FROM workflows
WHERE <claimable predicate>
ORDER BY next_attempt_at, workflow_id
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE workflows
SET lease_owner = ?,
    lease_expires_at = ?,
    fencing_token = fencing_token + 1
WHERE workflow_id = ?;
```

This is clearer and fairer than `UPDATE ... LIMIT 1`, and returns the
selected workflow and new fencing token reliably.

Recovery is not a separate mechanism: an expired lease on a claimable state
is simply claimable work. A dead worker's uncommitted Phase attempt vanished
with its connection, so the continuation the new claimant loads is exactly
correct.

### 24.3 Fenced publication

Every publication — Phase commit, reverse commit, completion, backoff
scheduling — must verify within its transaction:

```text
workflow_id
lease_owner
fencing_token
lease has not expired
expected lifecycle state
event ordering: arg_event_ts strictly > current_event_ts, enforced per append (§24.4)
```

If validation fails, the transaction aborts. A stale worker may continue
computing temporarily, but it cannot publish. Because lease validation and
effect publication are one atomic commit, fencing requires no distributed
coordination.

### 24.4 Time, ordering, and command discipline

MariaDB's clock remains the time **authority** for sourcing values — worker
wall clocks never determine ownership or due-times — but SQL is never the
time **generator**, and timestamps never carry ordering semantics.

```text
no AUTO_INCREMENT anywhere: identifiers derive deterministically from
  (current state, stable command input)

causal ordering is workflow-local:
  event_ts strictly greater than current_event_ts, enforced inside the fenced
  publication transaction

timestamps (event_ts, observed time, lease deadlines, next_attempt_at,
  created_at/updated_at, journal entry_ts) are explicit command
  parameters: sourced by the runtime (database clock in production,
  controlled clock in tests), FIXED across retries of the same command,
  stored unchanged — audit/scheduling values only

no NOW()/UTC_TIMESTAMP() or other ambient clock inside transition SQL
  logic or schema defaults; SQL never repairs or adjusts a supplied value

lease expiry is computed by the caller (database-sourced now + timeout)
  and supplied explicitly; claim/expiry/heartbeat comparisons compare
  against the explicitly supplied database-sourced now

state transitions are idempotent and deterministic from
  (current state, stable command input)

an already-committed command is resolved by its stable command ID
  BEFORE deriving or appending another event — a replayed command
  returns its recorded outcome and appends nothing
```

This preserves reproducibility without auto-generated IDs or ambient time:
replaying the same command stream against the same initial state produces
identical history, and `event_ts` — strictly monotonic per workflow by the append guard — is the order of
record.

### 24.5 Sticky execution, heartbeat, discovery

One claim spans many Phases:

```text
claim
  -> load pinned IR + continuation
  -> interpret pure code
  -> run apply (with §14.1 retry policy)
  -> commit (effects + checkpoint + continuation + lease heartbeat, one txn)
  -> loop until completion, block, or lease loss
```

Lease extension piggybacks on Phase commits; a standalone heartbeat UPDATE is
needed only when a single Phase runs long.

Work discovery uses polling with adaptive backoff against an index on the
claimable predicate. Notification channels are a later optimization.

Durable backoff state (`next_attempt_at`) lives on the workflow row, so a
backed-off workflow is not pinned to its current worker and survives
restarts.

### 24.6 Failure, cancellation, and audit

**Failure.** A Phase nonretryable failure or retry exhaustion transitions:

```text
forward -> reversing
```

published atomically with a disposition/event record, `next_attempt_at =
database_now`, and the reversal continuation at the top of the Checkpoint
stack. There is no separate `failed` state.

Milestone 1 does not expose an `on_failure: block` policy in the language or
IR. `blocked_resolution` remains available for reverse failures and explicit
resolution workflows. A pre-reversal approval policy can be added later when
a concrete domain requires it.

**Cancellation.** An authorized cancellation performs the transition
directly, rather than leaving a request flag for an executor to interpret.
Cancellation atomically performs:

```text
forward -> reversing
append cancellation event
record current disposition
next_attempt_at = database_now
set reversal continuation to top of Checkpoint stack
clear/revoke lease as appropriate
increment fencing token
```

The existing forward commit predicate already checks expected state and
fencing token, so no separate `cancel_requested` predicate is required.

The database serializes a cancellation racing a Phase commit:

```text
Phase commits first:
  cancellation observes the new Checkpoint and reverses it

Cancellation commits first:
  state/fencing changes, so the stale forward Phase publication
  fails and rolls back completely
```

This gives prompt cancellation with no intermediate states. Executors may
still check state between Phases as a fast path.

Cancellation is prompt at the transactional publication boundary, not an
asynchronous interruption. Pure computation or an open attempt may continue
briefly, but after cancellation wins the database race, that attempt cannot
publish.

Cancellation of `reversing` is an audited no-op. Cancellation of a terminal
or `blocked_resolution` Workflow returns an explicit non-applicable result.

Cancellation with an empty Checkpoint stack enters `reversing` and
immediately settles as `reversed`. Reporting distinguishes cancellation,
failure, and resolution through disposition/event data rather than
additional lifecycle states.

**Audit storage.** An append-only `workflow_events` table is the
authoritative audit trail. Only current operational fields are denormalized
onto `workflows`, such as:

```text
current_disposition
current_event_id
next_attempt_at
```

Every transition and its event commit atomically. Intervention events carry
a stable request ID, actor identity, reason, evidence, and timestamp,
allowing cancellation and resolution requests to be idempotent.

---

## 25. Open questions

### 25.1 Exact syntax

Current working syntax:

```phasedrift
workflow name(...) { ... }
phase name(...) -> CheckpointType { apply { ... } reverse(cp: CheckpointType) { ... } }
```

Need grammar decisions for:

```text
path interpolation: accounts/{id}/money
journal syntax
optional types: String? vs Optional<String>
imports/modules
host function namespaces
```

Resolved: record creation uses initialized `create(path, value)` returning a
writable Guard (§10.1); assignment never initializes a Guard.

### 25.2 Guard function names

MILE host candidates:

```text
rw
ro
peek
create
```

Alternative names:

```text
write
read
peek
create
```

### 25.3 Compensation strength metadata

Phases may need metadata describing reverse behavior:

```text
exact
accounting_reversal
exception_route
manual_required
irreversible_external
```

This can help classify which Workflows are fully auto-reversible and which may end in manual recovery.

### 25.4 Determinism restrictions

PhaseDrift should likely restrict or mediate:

```text
wall clock
randomness
network I/O
filesystem I/O
threads
unbounded loops
external calls
```

Host functions should expose controlled side effects as Guards or Phases.
§14.1 settles the prohibition of direct effects inside `apply`; remaining
open detail is time/randomness pinning (§25.9).

### 25.5 MILE replication model

MILE commits should be replication-friendly:

```text
monotonic revisions
hash chain
pull-after-seq
checkpoint/workflow state replication
snapshot chunks
idempotent apply on follower
```

This should be detailed in a separate MILE replication paper.

### 25.6 Workflow control flow

Initial Workflow control flow should support:

```text
if / else
match
successful early return
pure local loops and bounded collection operations
```

Local loops may compute inputs for a later Phase, but they must be
deterministic and side-effect-free. They may operate only on durable inputs and
produce typed serializable values.

The initial language should reject any control-flow cycle that can invoke a
Phase or effectful host capability. This excludes Phase calls from loops,
recursion, and collection callbacks while leaving ordinary local computation
available.

Example:

```phasedrift
val lines = load_invoice(invoice_id)

var total_cents = 0
for line in lines.items {
    total_cents += line.amount_cents
}

charge_invoice(lines.invoice_id, total_cents)
```

If execution crashes after `load_invoice` commits but before
`charge_invoice` commits, recovery resumes from the continuation following
`load_invoice`. The pure loop may execute again and must derive the same
`total_cents`.

The Checkpoint stack records what has committed and in what order it must be
reversed. The durable continuation separately records where execution resumes
and which values are needed there. Both are updated atomically at every Phase
commit.

### 25.7 Script revision migration (open point A)

Running Workflow instances pin to an immutable script revision (§22), but
"explicitly migrated" needs rules. The continuation stores an IR position,
which is meaningless in a different revision.

Candidate conservative stance:

```text
migration is legal only at a Phase boundary
the target revision must have a continuation-compatible position
  (same locals, same types)
reversal always uses the pinned revision; a target revision's reverse
  must not consume a foreign Checkpoint payload unless declared compatible
```

**Milestone 1 rule (adopted):** active Workflow instances are not migrated.
They finish or reverse using their pinned revision.

### 25.8 Guard escape prevention (open point B)

The continuation persists ordinary typed locals (§4.1), and Checkpoints hold
no runtime resources (§19.8). Proposed type-system rule (close to decision):

> Guard types are not serializable and cannot appear in Workflow scope,
> Checkpoint payloads, Phase results, or continuation values. Guards exist
> only inside an `apply` or `reverse` body.

This makes §19.8's principle a compile-time guarantee rather than a
convention.

### 25.9 Time/randomness pinning (open point C)

**Resolved** — see §24.4. Time and randomness inputs that influence committed
behavior are explicit command parameters, **fixed across retries** of the
same logical command/invocation: pinned when the command is created, not
re-sampled per attempt. The runtime sources them (database clock in
production, controlled clock in tests) and supplies them unchanged; they are
audit/scheduling values; `event_ts` (strictly monotonic per workflow) orders. Exposure
inside `apply` remains a host capability, not an ambient read.

### 25.10 Workflow failure and cancellation triggers (open point D)

§7.1 covers reversal, but what *enters* `reversing` needs definition:

```text
a Phase fails nonretryably or exhausts retries -> workflow declared failed
explicit external cancel
```

Open items:

```text
does the forward state machine distinguish failed -> reversing,
  or transition straight to reversing?
cancel honored only between Phases; a running Phase attempt completes
  or aborts, never interrupted mid-commit
cancel carries audited identity, like resolution operations (§7.1)
cancellation of a sleeping forward workflow must make it immediately
  claimable; represent as an audited cancellation request field or
  record, not by overloading lease state (§24.5)
```

To be settled next.

### 25.11 Worker/executor model (open point E)

**Resolved** — see §24. Lifecycle state is separate from lease ownership;
claims use `SELECT ... FOR UPDATE SKIP LOCKED` plus a fencing-token bump;
every publication is fence-validated inside its own transaction; database
time is authoritative.

---

## 26. Core statement

> PhaseDrift is a standalone typed lifecycle language for safe workflows. Workflows invoke Phases. A Phase applies atomic work and produces a Checkpoint. Guards protect unfinished work inside a Phase. If a Phase fails before commit, Guards roll back automatically. If a Workflow fails after Phases have committed, Checkpoints reverse automatically in reverse order. MILE Store is the first reference host runtime, providing durable atomic publish, record Guards, journals, checkpoint persistence, crash recovery, and replication.
