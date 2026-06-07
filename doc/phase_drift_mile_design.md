# PhaseDrift + MILE Reference Host Design

**Status:** Working design paper  
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
val refund = create(refunds/{refund_id})
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
```

All of these become visible together or none of them become visible.

There must never be:

```text
money moved but checkpoint missing
journal emitted but record not updated
workflow advanced but data missing
record created but phase not marked committed
```

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
create(path)
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

create(path)
  new-record Guard
  reserve key/assert nonexistence
  create only if Phase commits

journal { ... }
  staged journal/audit contribution
  committed atomically with Phase
```

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
        val refund = create(refunds/{refund_id})
        val policy = ro(refund_policies/current)

        assert amount_cents <= policy.max_auto_refund_cents
        assert parent_money.refundable_balance_cents >= amount_cents

        parent_money.refundable_balance_cents -= amount_cents

        refund = Refund {
            refund_id: refund_id
            parent: parent
            amount_cents: amount_cents
            state: RefundState.staged
            drawer: Drawer.refund_staging
        }

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

## 22. Open questions

### 22.1 Exact syntax

Current working syntax:

```phasedrift
workflow name(...) { ... }
phase name(...) -> CheckpointType { apply { ... } reverse(cp: CheckpointType) { ... } }
```

Need grammar decisions for:

```text
path interpolation: accounts/{id}/money
assignment to create(...) Guards
journal syntax
optional types: String? vs Optional<String>
imports/modules
host function namespaces
```

### 22.2 Guard function names

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

### 22.3 Compensation strength metadata

Phases may need metadata describing reverse behavior:

```text
exact
accounting_reversal
exception_route
manual_required
irreversible_external
```

This can help classify which Workflows are fully auto-reversible and which may end in manual recovery.

### 22.4 Determinism restrictions

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

### 22.5 MILE replication model

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

---

## 23. Core statement

> PhaseDrift is a standalone typed lifecycle language for safe workflows. Workflows invoke Phases. A Phase applies atomic work and produces a Checkpoint. Guards protect unfinished work inside a Phase. If a Phase fails before commit, Guards roll back automatically. If a Workflow fails after Phases have committed, Checkpoints reverse automatically in reverse order. MILE Store is the first reference host runtime, providing durable atomic publish, record Guards, journals, checkpoint persistence, crash recovery, and replication.

