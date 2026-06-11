# storage-portability

## Purpose and status

This note records a forward-looking storage constraint for Microflows: MariaDB
and stored procedures are the current persistence implementation, but they must
not become inseparable from the workflow semantics.

The future storage engine may be a distributed, comparatively simple durable
store with conditional writes, such as an object store. No replacement engine,
migration, schema, serialization format, or deployment architecture is selected
here. The immediate goal is to preserve an extractable durable-transition model
while the runtime behavior is still taking shape.

This is architectural guidance and a scheduled review item, not active storage
implementation work.

## Bigger picture

Microflows owns durable coordination state, not participant business data. Its
essential persistence responsibilities are:

- identify one workflow aggregate by `workflow_id`;
- record its state, direction, disposition, continuation, and lease/fence;
- durably bind operation requests before dispatch;
- record authoritative operation results;
- maintain the compensation checkpoint stack;
- append causally ordered audit events;
- make retries and recovery idempotent;
- expose claimable work without making a discovery index authoritative.

Those semantics should survive a change in storage engine. MariaDB transactions,
row locks, indexes, foreign keys, and stored-procedure syntax are mechanisms used
today to implement them. They are not the desired public contract of the
coordinator.

The architectural boundary is therefore a **durable transition API**. A caller
submits a command against one workflow instance with an expected version or
fencing condition. The storage implementation atomically either:

- accepts the command and publishes the new aggregate state plus its events;
- reports that the command was already applied and returns the durable result;
- rejects it because the aggregate state does not permit the transition;
- rejects it because ownership/version/fencing has changed.

The current stored procedures should be understandable as implementations of
such commands.

## Target conceptual model

At the conceptual level, a portable transition resembles:

```text
load(workflow_id) -> aggregate, storage_version

decide(aggregate, command) ->
    accepted(new_aggregate, events, result)
  | already_applied(durable_result)
  | invalid_transition(reason)

compare_and_swap(
    workflow_id,
    expected_storage_version,
    new_aggregate,
    events
) -> committed(new_storage_version) | conflict
```

The exact API need not literally expose load/decide/CAS as separate calls. A
MariaDB procedure can perform them in one transaction. A future object-store
implementation might conditionally replace a workflow manifest using an ETag or
generation number while writing immutable records. The important point is that
the command semantics and atomic ownership boundary remain explicit.

## Aggregate boundary

The preferred atomic ownership boundary is one workflow instance.

State required to decide and commit a workflow transition includes the workflow
head and the operation/checkpoint/event records belonging to that workflow.
These may remain separate relational rows today and may become one manifest plus
immutable referenced objects later. Their physical layout is secondary to the
logical rule:

> A correctness-critical transition must not require an atomic transaction
> spanning unrelated workflow instances.

Cross-workflow indexes, due-work discovery, metrics, and reporting should be
treated as derived infrastructure. They may be stale, duplicated, or rebuilt
without changing the authoritative state of a workflow.

Participant state remains outside this aggregate. Microflows never relies on an
atomic transaction spanning its workflow state and participant business state;
stable operation identities and reconciliation bridge that boundary.

## Required portable guarantees

Any future persistence implementation must be able to provide the following
semantics, even if its physical mechanisms differ from MariaDB:

- Atomic publication of one workflow transition and its causal audit evidence.
- Conditional update by aggregate version and, where applicable, fencing token.
- Stable command or invocation identities for idempotent replay.
- Monotonic causal event ordering within a workflow instance.
- Durable operation request identity before an external dispatch occurs.
- Durable result/checkpoint/continuation advancement as one logical transition.
- Durable reversal intent and compensation progress before external dispatch.
- Lease expiry and duplicate claim tolerance without allowing a stale owner to
  commit.
- Explicit conflict, fence-loss, invalid-state, and already-applied outcomes.
- Recovery after process failure at every external-dispatch boundary.

Wall-clock timestamps are evidence and scheduling inputs, not causal identity.
Event sequence or aggregate version remains the ordering authority.

## Stored procedures as transition adapters

Every correctness-bearing stored procedure should be reviewable as a transition
adapter with a small, documented contract:

- **Command identity:** What makes retries of this command the same command?
- **Aggregate:** Which single workflow instance does it mutate?
- **Preconditions:** Which state, direction, disposition, operation/checkpoint
  status, version, and fencing conditions must hold?
- **Atomic write set:** Which workflow-owned facts change together?
- **Events:** Which causal events are appended, with what deduplication rule?
- **Return outcomes:** How are success, already-applied, conflict, invalid state,
  fence loss, and clock/scheduling errors distinguished?
- **Idempotency:** What durable result is returned on replay?
- **External boundary:** What must be committed before or after participant I/O?
- **Consistency requirement:** Does this need compare-and-swap, append-if-head,
  unique-create, or only an eventually consistent read?

Host APIs should expose these domain outcomes rather than database error codes
or MariaDB-specific concepts.

## Procedure classification

During the portability review, classify every stored procedure into one of these
groups:

1. **Aggregate transition**

   A correctness-bearing command against one workflow, such as create, claim,
   request, settle, defer, begin reversal, or settle compensation. These should
   map cleanly to conditional aggregate updates.

2. **Aggregate read**

   Inspection or durable request/result lookup by workflow identity. These must
   define whether they require the latest committed head or may tolerate stale
   reads.

3. **Discovery or derived index**

   Finding due/claimable workflows, reporting, metrics, or operational search.
   These may require a secondary index or queue in a distributed design, but the
   index must not become the authority for workflow state.

4. **Cross-aggregate invariant**

   Any operation whose correctness requires atomically reading or writing
   multiple workflow instances. This is a migration warning and should be
   redesigned or justified explicitly.

5. **Storage utility**

   Database clock reads, migration helpers, and implementation-specific
   maintenance. These are replaceable mechanisms, not domain transitions.

## What “too fat” means

A procedure is not too fat merely because it updates several tables. Updating a
workflow head, its operation/checkpoint record, and its event in one transaction
may be exactly the atomic domain transition required.

A procedure becomes suspiciously storage-bound when it:

- combines several independent domain commands that need not be atomic;
- derives workflow semantics through broad SQL joins instead of an explicit
  command contract;
- mutates multiple unrelated workflow instances;
- depends on global counters or database-generated ordering for correctness;
- mixes authoritative transition logic with discovery, reporting, or cleanup;
- relies on lock duration or transaction isolation behavior that is not stated as
  a domain precondition;
- returns an implicit result that can only be understood from affected-row counts
  or vendor-specific errors;
- performs unbounded scans or loops inside the atomic write;
- makes a secondary index or denormalized view part of the correctness proof;
- embeds policy that should live in the runtime decision layer and cannot be
  expressed as a deterministic transition.

Conversely, splitting one required atomic transition across several calls would
make portability and correctness worse. The review should seek **cohesive**
procedures, not mechanically small procedures.

## Guidance for current work

Until the formal review:

- Keep new procedures scoped to one `workflow_id`.
- Make transition preconditions and outcomes explicit in names, parameters,
  tests, and host variants.
- Preserve stable operation, request, checkpoint, and reverse-invocation
  identities.
- Append audit evidence in the same logical commit as the state transition it
  describes.
- Avoid making claim scans or indexes authoritative.
- Avoid cross-workflow transactions and correctness dependencies on global SQL
  queries.
- Keep participant I/O outside database transactions.
- Prefer absolute persisted deadlines; compute relative policy delays before the
  transition is committed.
- Treat fencing/version conflicts as normal domain outcomes, not exceptional
  database failures.
- Add SP regression tests around transition contracts, especially replay,
  fence-loss, invalid-state, and partial-progress boundaries.

Reversal work should follow these constraints. Its procedures may atomically
update the workflow, one checkpoint, and an event because those are one workflow
aggregate transition. It should not introduce cross-workflow coupling or hide
participant dispatch inside a stored procedure.

## Future distributed-store shape

One plausible future mapping, included only to test the abstraction, is:

- an authoritative workflow head/manifest addressed by `workflow_id`;
- a storage generation or ETag used for conditional replacement;
- immutable operation, checkpoint, and event objects referenced by the head;
- unique-create semantics for stable command/invocation records;
- a rebuildable due-work index or queue populated from committed heads;
- workers that tolerate duplicate or stale discovery and revalidate against the
  authoritative workflow head before committing;
- compaction or snapshotting that preserves causal history and idempotency facts.

This is not a commitment to S3 or to an event-sourced design. It demonstrates the
kind of simple distributed primitives the transition contracts should be able to
use: conditional write, immutable create, object read, and rebuildable indexing.

An object store alone does not provide relational transactions. If workflow
state is physically split across objects, publication must still have one
authoritative commit point so readers never treat partially written transition
artifacts as committed state.

## Planned review point

Perform a dedicated storage-portability audit:

1. After the dispatcher, reversal, recovery, and manual-IR runtime semantics have
   settled enough that the real transition set is visible.
2. Before investing in the parser/type checker and frontend, so storage-specific
   assumptions are corrected before the language makes them harder to change.

The audit should:

- inventory every procedure and host method;
- write the domain command contract for each;
- classify each procedure using the categories above;
- identify cross-aggregate or MariaDB-specific correctness dependencies;
- decide which policy belongs in the runtime versus the persistence adapter;
- sketch a non-relational implementation of each aggregate transition;
- refactor procedures or host boundaries that cannot be mapped coherently;
- add missing contract tests independent of incidental SQL behavior;
- update the authoritative design with the resulting storage-neutral model.

The audit is successful when every correctness-bearing procedure can be explained
as a cohesive command over one workflow aggregate, and when claim/discovery
infrastructure can be replaced without changing workflow truth.

## Decisions

- MariaDB remains the current implementation; no migration is underway.
- Stored procedures are private persistence adapters for durable domain
  transitions, not the permanent architecture or public runtime API.
- One workflow instance is the preferred atomic aggregate boundary.
- Cross-workflow atomic invariants are prohibited unless explicitly reviewed.
- Discovery indexes and queues are rebuildable and non-authoritative.
- Procedure size is judged by domain cohesion and portability, not line count or
  number of workflow-owned tables touched.
- The formal procedure audit happens after core runtime semantics settle and
  before parser/type-checker implementation.

## Intentionally open

- The future storage engine and deployment topology.
- Whether the physical representation is a single aggregate document, a manifest
  plus immutable objects, an event log with snapshots, or another model.
- How due-work discovery is implemented and repaired.
- How large workflow histories are compacted, archived, or retained.
- Which reads require strong consistency.
- Whether transition decisions remain server-side, move into the runtime with
  conditional writes, or use a hybrid adapter.
- Migration and dual-write strategy, if a migration is ever chosen.

## Current status and next action

**Recorded for alignment; no storage migration is scheduled.** Apply these
constraints to new reversal procedures. Once reversal and the remaining manual-IR
runtime machinery settle, perform the SP/host transition audit before beginning
parser/type-checker work.
