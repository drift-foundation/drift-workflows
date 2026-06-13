# Microflows storage portability (forward-looking architectural guidance)

This note records the storage-neutral durable model for Microflows so the persistence
layer stays separable from workflow semantics. It is architectural guidance, not a
migration plan. **MariaDB and stored procedures are the current implementation and no
migration is scheduled or underway.** No replacement engine, schema, serialization
format, or deployment topology is selected here.

The future storage engine may be a comparatively simple distributed durable store with
conditional writes (e.g. an object store). The goal is to keep an extractable durable-
transition model while runtime behavior is built — and, having performed the SP/host
transition audit (2026-06, post-ScriptRegistry, before the parser), to record its result
as the authoritative model.

## Core principle

Microflows owns durable *coordination* state, not participant business data. MariaDB
transactions, row locks, indexes, foreign keys, and stored-procedure syntax are
mechanisms used to implement that coordination today. They are **not** the public
contract of the coordinator. The contract is a **durable transition API**: a caller
submits a command against one workflow instance with an expected version/fencing
condition, and the storage implementation atomically either accepts and publishes the new
aggregate state plus its events, reports the command already applied (returning the
durable result), rejects it because the aggregate state forbids the transition, or rejects
it because ownership/version/fencing changed.

Audit result: the current stored procedures already implement exactly such commands.

## Aggregate boundary

The atomic ownership boundary is **one workflow instance** (`workflow_id`). The workflow
head plus the operation, checkpoint, and event records belonging to that workflow are one
aggregate. Their physical layout (separate relational rows today; possibly a manifest plus
immutable referenced objects later) is secondary to the rule:

> A correctness-critical transition must not require an atomic transaction spanning
> unrelated workflow instances.

Cross-workflow indexes, due-work discovery, metrics, and reporting are **derived
infrastructure** — they may be stale, duplicated, or rebuilt without changing the
authoritative state of any workflow. Participant state is outside the aggregate;
Microflows never relies on an atomic transaction spanning its state and participant
business state — stable operation identities and reconciliation bridge that boundary.

**Audit confirmation:** of 21 procedures, there are **0 cross-aggregate (atomic) writes**.
Two procedures span workflows, both via a rebuildable derived index rather than authority:
`sp_mf_plan_stalled` is a pure cross-workflow *read*, while `sp_mf_workflow_claim` does
cross-workflow *discovery* and then a *single-aggregate mutation* (it installs the lease on
the one chosen workflow). The discovery scan is non-authoritative in the portable design;
note, though, that today it is lock-authoritative within its transaction (the chosen row is
held by `FOR UPDATE` — see Known portability debt).

## The four storage primitives

Every correctness-bearing procedure reduces to four primitives. A relational adapter
performs several in one transaction; an object-store adapter performs them as conditional
writes against one aggregate manifest.

1. **unique-create(key, initial) → Created | Exists(current).** Insert-if-absent on the
   aggregate key; on conflict return the *current durable value* so a racing creator adopts
   the winner. Used by workflow creation and by the create-once identities (`operation_id`,
   `reverse_invocation_id`, `reversal_trigger_operation_id`).
2. **commit(key, expect, set, transition_child, create, append) → Committed(new_version) |
   AlreadyApplied(result) | FenceLost | InvalidState(reason) | Conflict.** A conditional head
   replacement: verify the expected head `storage_version` (the ETag) together with the
   domain precondition `(lease_owner, fencing_token, state)`; write the new head fields;
   record any **child transition** (see below); unique-create any new immutable child
   objects; and, when the transition produces audit evidence, **append the event(s) with
   `event_seq = current_event_seq + 1`** — all in one commit. The workhorse: every aggregate
   transition is one such commit.

   **Three distinct per-aggregate counters — do not conflate them.** `storage_version` is the
   optimistic-concurrency ETag; it changes on *every* head mutation, **including lease-only
   ones (claim, heartbeat, release, defer) that append no event.** `current_event_seq` is
   *only* causal event order and advances *only* when an event is appended. `fencing_token` is
   the publish authority and is bumped *only* on claim. In particular `current_event_seq`
   **cannot** serve as the head ETag — a lease mutation leaves it unchanged — so a
   conditional-write store needs a dedicated `storage_version`. (MariaDB has no such column
   today; it relies on the row lock instead — see Known portability debt.)

   **Child transitions.** Some transitions also advance an existing child: `operation_settle`
   moves an operation requested→settled and records its result; `reverse_request` binds a
   checkpoint's durable compensation contract before dispatch; reverse settle/block moves a
   checkpoint's reversal state. Children are otherwise immutable, so the portable form models
   each such change as a **write-once immutable object selected by the manifest** (never an
   in-place edit of an existing immutable object):
   - settle writes an immutable **result** object keyed by `operation_id` (operation status is
     then *derived* — "settled" ⇔ a result object exists);
   - `reverse_request` writes the **reverse binding** — an immutable, write-once object holding
     the pinned compensation contract (`reverse_invocation_id`, operation/version, input +
     hash), keyed by checkpoint `seq`; it is created BEFORE compensation dispatch and is
     content-matched (never overwritten) on replay (binding "set" ⇔ a binding object exists);
   - reverse settle/block writes an immutable **checkpoint-reversed / -blocked** terminal record
     (active-stack and reversal state derived from which terminal record exists).

   The head CAS flips the manifest's child references atomically with the event append. A
   relational adapter does the equivalent as in-place child `UPDATE`s in the same transaction;
   either way the child write is part of the one aggregate commit and gated by the head
   `storage_version` (equivalently, an explicit child-CAS precondition such as
   `op_status = requested` or `reverse_binding IS NULL`).
3. **read(key, consistency) → aggregate | NotFound.** A point read; each call declares
   whether it needs the latest committed head or tolerates staleness. None require a lock.
4. **rebuildable derived index** (due-work, stalled). Maintained from committed heads; may be
   stale or rebuilt. In the portable design a worker that pops a candidate **revalidates
   against the authoritative head via a fenced commit** (the claimable predicate carried in
   `expect`) before publishing — so the index is never a source of truth. (Note: the current
   MariaDB claim does NOT yet do this point-CAS revalidation; it relies on the row lock — see
   Known portability debt.)

Plus an **edge clock**: one authoritative `now` is sourced once (the single sanctioned
clock read) and threaded as fixed command input. Transition logic never reads a clock, so
it stays deterministic and replayable; wall-clock values are evidence and scheduling
input, never causal identity.

## Invariants that must survive any engine

- **I1 — single-aggregate atomicity.** A transition writes only one workflow's head and its
  own children.
- **I2 — `event_seq` is the only causal order.** `current_event_seq` is its head
  projection; appends derive `event_seq = current_event_seq + 1` inside the commit.
  Timestamps never order anything; nothing is `AUTO_INCREMENT` and no column has a
  `DEFAULT`/`ON UPDATE` time. Causal order is **distinct from the head's optimistic-
  concurrency version** (`storage_version`): a lease-only head mutation (claim, heartbeat,
  release, defer) advances the head version but appends no event and so leaves
  `current_event_seq` unchanged.
- **I3 — fencing.** `fencing_token` is a per-aggregate monotonic counter bumped only on
  claim (and direct intervention). A publish requires the matching token; a stale holder
  can compute but never commit.
- **I4 — idempotency keys.** `workflow_id` (create), `request_id` (event log),
  `operation_id` + `input_hash` (operation), `content_hash` (plan pin),
  `reverse_invocation_id` (compensation), `reversal_trigger_operation_id` (reversal). Each
  makes a replayed command the *same* command.
- **I5 — request-before-dispatch / result-after-success.** Durable operation/compensation
  identity is committed *before* external I/O; the authoritative result is read back
  *before* re-authorizing — a settle/reverse-settle returns the stored result **before** the
  fence check, so a lost-ack retry with a dead token still resolves to `already_*`.
- **I6 — immutability.** Plan pin, checkpoint `payload` and reverse binding, and event rows
  are write-once; recovery never re-reads mutable data to reconstruct a decision.
- **I7 — terminal/blocked reachability is derived from the aggregate, not a clock.**
  `reversed` ⇔ no active checkpoint remains. `blocked_resolution` is reached on EITHER path,
  qualified by `execution_direction`: on the **forward** path a definite `operation_fail`
  (direction stays forward), or on the **reverse** path a top compensation that failed or is
  indeterminate (direction stays reverse). The lease is cleared on every terminal/blocking
  transition.

## Storage-neutral semantics vs MariaDB mechanism

The left column is the public contract; the right column is replaceable implementation.
The host already hides the right column behind domain-outcome variants (and deliberately
avoids affected-row-count semantics — e.g. heartbeat/release never use `ROW_COUNT()`).

| Domain semantics (must survive) | MariaDB mechanism (incidental) |
|---|---|
| unique-create on aggregate key | `1062` duplicate CONTINUE HANDLER (not `INSERT IGNORE`) |
| fenced compare-and-swap | locking `SELECT … FOR UPDATE` → in-proc compare → `UPDATE` |
| append-if-head (`seq = cur+1`) | second statement in the same transaction |
| precondition predicate | the row already locked when the `UPDATE` runs |
| per-aggregate stack cursor | `MAX(seq)` / `ORDER BY seq DESC LIMIT 1` over one workflow |
| due-work / stalled discovery | `idx_mf_workflow_claim` + `SKIP LOCKED`; `JSON_EXTRACT` scan |
| absent-row outcome | `NOT FOUND` CONTINUE HANDLER |
| argument validation | `SIGNAL SQLSTATE '45000'` |
| structural "no orphan child" | `fk_mf_*_workflow` foreign keys |
| state-machine legality | `ck_mf_workflow_state_*` CHECK constraints (mirror `state.drift`) |
| result serialization | `JSON_OBJECT`, `LOWER(HEX())`, `CAST(… AS SIGNED)`, `DATE_FORMAT` |
| edge clock | `NOW(6)` + `DATE_ADD` |

## Procedure classification and current result

Classify every procedure into one of: **(1)** aggregate transition, **(2)** aggregate
read, **(3)** discovery / derived index, **(4)** cross-aggregate invariant (a migration
warning — must be redesigned or explicitly justified), **(5)** storage utility. Current
result (21 procedures): **13 transitions, 5 reads, 2 derived indexes, 0 cross-aggregate,
1 utility.**

| Procedure | Class | Portable form |
|---|---|---|
| `sp_mf_workflow_create` / `…_create_planned` | 1 | unique-create (planned returns current pin) |
| `sp_mf_workflow_claim_by_id` | 1 | fenced CAS on lease + fencing bump |
| `sp_mf_workflow_release` / `…_heartbeat` | 1 | fenced CAS on lease (heartbeat requires unexpired) |
| `sp_mf_operation_dispatch_defer` | 1 | fenced CAS on lease+schedule + dedup append |
| `sp_mf_operation_request` | 1 | fenced CAS + unique-create op + append-if-head |
| `sp_mf_operation_settle` | 1 | CAS op-status 1→2 + unique-create checkpoint + head CAS |
| `sp_mf_operation_fail` | 1 | fenced CAS head 1→3 + append |
| `sp_mf_workflow_begin_reversal` | 1 | create-once trigger + fenced CAS head + append |
| `sp_mf_checkpoint_reverse_request` | 1 | create-once binding + fenced CAS + append |
| `sp_mf_checkpoint_reverse_settle` / `…_block` | 1 | CAS checkpoint state + head CAS + append |
| `sp_mf_workflow_inspect` / `…_request_get` / `…_operation_result` / `sp_mf_plan_get` | 2 | point read |
| `sp_mf_checkpoint_reverse_head` | 2 | per-aggregate ordered read (stack cursor) |
| `sp_mf_workflow_claim` | 3 | rebuildable due-work index; today scan-under-lock (portable: + point-CAS) |
| `sp_mf_plan_stalled` | 3 | rebuildable stalled index (non-authoritative) |
| `sp_mf_clock_read` | 5 | edge clock source (once per command) |

A procedure is **not** "too fat" merely for touching several tables — updating a workflow
head, its operation/checkpoint, and its event in one transaction is exactly the atomic
domain transition required. A procedure is suspiciously storage-bound when it combines
independent commands that need not be atomic, derives semantics through broad joins instead
of an explicit command contract, mutates multiple workflow instances, depends on global
counters or DB-generated ordering for correctness, mixes transition logic with
discovery/reporting/cleanup, relies on lock duration or isolation behavior not stated as a
domain precondition, returns an implicit result readable only from affected-row counts or
vendor errors, performs unbounded scans inside the atomic write, makes a secondary index
part of the correctness proof, or embeds policy that belongs in the runtime decision layer.
The review seeks **cohesive** procedures, not mechanically small ones. (Audit: none of the
current procedures are too fat by this test.)

## Portable command/storage interface

```text
Aggregate(workflow_id):
  head     = { storage_version,                      # ETag: bumped on EVERY head write
               state, direction, disposition, current_event_seq, fencing_token,
               lease_owner, lease_expires_at, next_attempt_at, continuation,
               reversal_trigger?, plan_pin?, active_stack_top?,
               op_refs[], checkpoint_refs[] }         # manifest selects current child versions
  events[] = immutable, ordered by event_seq (causal authority; advances only on append)
  ops[]    = immutable request + write-once result object (status "settled" ⇔ result exists)
  ckpts[]  = immutable payload + write-once reverse binding + write-once terminal record
             (reversal state derived from which terminal record exists)

Primitives:
  create(workflow_id, head0, child0…)  -> Created | Exists(current_head)
  read(workflow_id, consistency)       -> Aggregate | NotFound        # latest | stale_ok
  commit(workflow_id,
         expect = { storage_version,                 # head ETag — concurrency control
                    fencing_token?, state?,          # publish authority + domain precondition
                    op_status?, ckpt_state? },        # optional child-CAS precondition
         set    = head-deltas,                        # incl. new storage_version, child refs
         transition_child = op_settled(operation_id, result)            # write-once result
                          | ckpt_bound(seq, reverse_binding)            # write-once binding
                          | ckpt_reversed(seq) | ckpt_blocked(seq, disposition)  ?,
         create = immutable child objects,
         append = events[ event_seq = current_event_seq+1 ])   # only when evidence is produced
        -> Committed(new_storage_version)
         | AlreadyApplied(stored_result)        # idempotent replay, read BEFORE fence
         | FenceLost | InvalidState(reason) | Conflict
  clock() -> now                                # sourced once at the edge, then fixed input

Derived (non-authoritative, rebuildable from heads):
  due_work_index.pop(script_name, now) -> candidate workflow_id   # then revalidate via commit
  stalled_index.list()                 -> heads whose latest event is a cleared-lease
                                          revision_unavailable deferral
```

The `MicroflowsHost` outcome variants are already this vocabulary (domain outcomes, not DB
codes). Per-command `expect` predicates (illustrative): `claim_by_id` = `state∈{1,2} ∧ due
∧ (unleased∨expired)`; `heartbeat` = `owner ∧ token ∧ unexpired ∧ state∈{1,2}`;
`operation_request` = `owner ∧ token ∧ state=1 ∧ plan-order`; `operation_settle` =
`(replay: op_status=2 first) ∧ owner ∧ token ∧ state=1`; `reverse_settle` = `reverse_id
match ∧ top-of-stack ∧ owner ∧ token ∧ state=2`.

## How the required guarantees are preserved

- **Fencing** → `commit`'s `expect.fencing_token`, a **domain** publish-authority condition.
  The token lives on the head, is bumped only by the claim CAS, and gates every publish; a
  stale holder's commit returns `FenceLost`. It is distinct from the head `storage_version`
  (the concurrency ETag): fencing answers "may THIS worker publish?", the ETag answers "has the
  head changed since I read it?".
- **Idempotency** → `create`-returns-current for genesis and the create-once identities, and
  the `AlreadyApplied(stored_result)` arm of `commit` for settles. The identity keys (I4) are
  unique-create constraints in any store, and the already-applied result is read **before**
  the fence so dead-token retries still resolve.
- **Ordering** → `current_event_seq` is the **causal event-order** authority
  (`append: event_seq = current_event_seq+1`) and advances *only* when an event is appended. It
  is **not** the concurrency-control version: optimistic concurrency uses the head's
  `storage_version` (bumped on every head write, including lease-only mutations that append no
  event), and publish authority uses `fencing_token` (bumped only on claim). Three separate
  per-aggregate counters; a conditional-write store carries `storage_version` as the head ETag.
- **Durable replay / recovery** → request-before-dispatch (I5) + immutable evidence (I6) give
  every external-dispatch boundary a durable pre-image (`*_request_get`, `reverse_head`) and a
  durable authoritative result (`operation_result`, settle replay). Recovery re-derives the
  cursor from the aggregate (checkpoint stack / continuation projection), never from a clock or
  a derived index.

## Known portability debt (watch items)

These are the only places the current implementation leans on MariaDB beyond mechanical
translation. None is a defect today; each is a contract to make explicit before a
non-relational port.

- **Lock duration as an unstated precondition (cross-cutting).** Every mutating procedure
  does locking-SELECT → compare → UPDATE and relies on holding the InnoDB row lock across both
  statements. Re-express each transition's predicate as an explicit CAS condition carried in
  the write, so correctness survives a store with no long-lived row locks. (No behavior change
  in MariaDB.)
- **Discovery scans and the claim authority.** `sp_mf_workflow_claim` (`SKIP LOCKED` + ordered
  index) and `sp_mf_plan_stalled` (cross-workflow + in-payload JSON predicate) are rebuildable
  indexes in spirit — but the current claim is **not yet** a revalidating point-CAS: today the
  locking `SELECT … FOR UPDATE` holds the chosen row and the follow-up `UPDATE` re-touches by
  `workflow_id` only (the claimable predicate is asserted in the SELECT, never re-checked in the
  UPDATE), so the scan-under-lock is **authoritative within the transaction**. The portable
  design must ADD point-CAS revalidation — pop a candidate from the rebuildable due-work index,
  then claim via a head CAS whose `expect` carries the claimable predicate — which the current
  implementation does not do. A portable stalled index would likewise satisfy the
  `revision_unavailable` predicate at event-append time rather than by scanning payloads.
- **Checkpoint-stack cursor.** `reverse_head` and the `MAX(seq) WHERE reversal_state=1`
  top-checks derive the stack top by a *per-aggregate* ordered scan (correct, single-workflow).
  Materializing an `active_stack_top` pointer on the head removes the only ordered-scan
  dependency.
- **`create_planned` race resolution.** Becomes a single conditional-put whose conflict path
  returns the current value (unique-create-returns-current), rather than relying on the PK lock
  being held until COMMIT.

## Object-store shape (abstraction test, not a commitment)

A head/manifest per `workflow_id` (the mutable workflow fields + `active_stack_top` + an
embedded-or-referenced plan pin), conditionally replaced by its generation/ETag — the head's
`storage_version` (which advances on every head write, including lease-only ones), NOT
`current_event_seq` (causal event order, which a lease mutation leaves unchanged). Immutable
referenced objects: one event object per
`event_seq` (the authority — ordered by sequence, never by time), per-operation
request/result objects keyed by `operation_id`, checkpoint objects with write-once
payload/binding. A publication is one conditional PUT of the head that co-writes the new
immutable child(ren); if the store cannot co-write atomically, the head flip is the single
commit point and children are written first under content-addressed keys so a partial write
is invisible until the head references it. Due-work and stalled indexes are separate
rebuildable structures populated from head transitions and revalidated on pop. The clock is a
coordinator service call. This demonstrates the kind of simple distributed primitives the
transition contracts must be able to use — conditional write, immutable create, object read,
rebuildable indexing — without committing to any specific product or to event sourcing.

## Decisions

- MariaDB remains the implementation; no migration is underway.
- Stored procedures are private persistence adapters for durable single-aggregate
  transitions, not the permanent architecture or the public runtime API.
- One workflow instance is the atomic aggregate boundary; cross-workflow atomic invariants
  are prohibited unless explicitly reviewed.
- Discovery indexes and queues are rebuildable and non-authoritative.
- Procedure size is judged by domain cohesion and portability, not line count or number of
  workflow-owned tables touched.
- Host APIs expose domain outcomes, never database error codes or affected-row semantics.

## Guidance for new work

- Keep new procedures scoped to one `workflow_id`; make preconditions and outcomes explicit
  in names, parameters, tests, and host variants.
- Carry the transition's precondition as an explicit condition (not an implicit lock); append
  audit evidence in the same logical commit as the transition it describes.
- Preserve stable operation, request, checkpoint, and reverse-invocation identities; treat
  fencing/version conflicts as normal domain outcomes.
- Keep participant I/O outside database transactions; prefer absolute persisted deadlines and
  compute relative policy delays before the transition commits.
- Avoid making claim scans or indexes authoritative; avoid cross-workflow transactions and
  correctness dependencies on global queries.
- Add SP regression tests around transition contracts — especially replay, fence-loss,
  invalid-state, and partial-progress boundaries.

## Intentionally open

The future storage engine and deployment topology; whether the physical representation is a
single aggregate document, a manifest plus immutable objects, or an event log with snapshots;
how due-work discovery is implemented and repaired; how large histories are compacted or
retained; which reads require strong consistency; whether transition decisions stay
server-side, move into the runtime with conditional writes, or use a hybrid adapter; and
migration/dual-write strategy if a migration is ever chosen.
