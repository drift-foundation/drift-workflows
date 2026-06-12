# script-registry  (roadmap §7 step: manual portable IR → ScriptRegistry)

## Short-term objective
Replace the config-embedded forward plan with **immutable, versioned, manually
constructed IR resolved by `(script_name, script_revision)`** through a
`ScriptRegistry` abstraction. Preserve the existing runner loop, plan pinning,
dispatch, recovery, and reversal machinery unchanged. **Parser stays deferred** —
the IR remains hand-built until the execution + storage contracts are stable.

## Current behavior / problem
The manual IR lives in **mutable executor config**: the runner's `_build_plan`
reads a `plan` array and resolves each step through the `operations` registry, all
from the JSON config. A workflow pins `(script_name, script_revision)` — today the
constants `protocol_spike`/`1` — plus a per-step `plan_hash` in
`tb_mf_workflow_plan` (with `plan_length`), validated atomically by
`sp_mf_workflow_create_planned` (`plan_conflict` on divergence).

What's missing: a **named, versioned, immutable script identity**. There is no
registry of revisions, no deployment/activation/rollback story, and no guarantee
that `(name, revision)` resolves to one frozen IR that a running instance can pin
and a resuming worker can re-load without substitution.

## Accepted design decisions (from microflows_design.md §10/§22)
- **Immutable IR registry keyed by `(script_name, revision, content_hash)`.** A
  revision, once active, is never mutated. Manifest + scripts (here: the
  hand-built IR set) are one deployment unit.
- **Manual IR for now.** Registry entries are hand-constructed `ScriptIR` objects
  (the ordered plan + per-operation participant/schema/compensation bindings —
  exactly today's `plan` + `operations`, just owned by the registry). No parser.
- **Revision pinning, never substitution.** A NEW workflow pins the **active**
  revision (as seen by its creating executor); a RUNNING workflow keeps its
  **pinned** revision; old IR is retained while still referenced; compensation uses
  the workflow's pinned revision. If the deployment lacks a workflow's pinned
  revision, Microflows **durably defers** (repairable, like
  `pinned_contract_unavailable`) — it MUST NEVER run a different revision.
- **The pin is decided by durable CREATION, not by the active pointer (resolves the
  fresh-vs-resume contradiction).** The runner reads its active `(name, rev, hash)`
  only as the value to pin on a FRESH create. On replay of an EXISTING workflow,
  `create_planned` RETURNS the committed pinned `(name, rev, hash)` — it never
  conflicts merely because the caller's active revision has since moved (N → N+1).
  The runner then loads the **returned** revision, not its active one. A workflow
  pinned to N therefore resumes after N+1 is activated. Conflict is reserved for a
  CONTRADICTORY immutable identity: a different `script_name`, or the SAME
  `(name, revision)` presented at a DIFFERENT `content_hash` (a revision edited under
  it — a deployment error), → `plan_conflict`.
- **Activation is process-local; the workflow's pin is the consistency boundary.**
  An in-process immutable registry swaps atomically within ONE executor; across a
  fleet, executors may briefly hold different active revisions during a rollout. We
  do NOT require a fleet-wide atomic active pointer. Instead the DURABLE workflow
  creation decides the pin: whichever executor wins the `create_planned` PK INSERT
  freezes ITS active revision onto the workflow, immutably. Two fresh workflows
  created mid-rollout may pin N vs N+1 — each internally consistent and resumable on
  any executor that carries its revision. The deployment guarantee (§10) is the
  invariant: **every executor must provide every revision its active workflows
  need.** (A fleet-wide / DB-backed active pointer is a later `ScriptRegistry`
  implementation, not milestone 1.)
- **Staged, atomic (per-process) reload.** Adding/activating a revision never edits
  existing ones; a reload that fails any declared script leaves that process's
  active registry untouched; the swap is atomic within the process. **Rollback** =
  re-activate a known-good revision (and, durably, new workflows then pin it again).
- **Behind a `ScriptRegistry` interface** so a filesystem-manifest or DB-backed
  implementation can replace the milestone-1 one without touching the executor.
- **The plan pin EVOLVES into the registry content hash (approved) — layered.** The
  per-step `plan_hash` was a stand-in for exactly this immutable-revision identity.
  Replace it: `tb_mf_workflow_plan` pins the COMPLETE durable identity
  `(script_name, script_revision, content_hash, plan_length)`. The layer split:
  - **`create_planned`** (storage layer): atomically PIN the supplied identity on a
    fresh create; on replay RETURN the FULL stored pin — `(name, revision,
    content_hash, plan_length)` — and compare only SUPPLIED-vs-STORED, never the
    registry. Returning `plan_length` matters when the active revision's length
    differs from the stored one: terminal replay needs the STORED final sequence, and
    resume validates the resolved IR's length against it.
  - **Runner** (registry layer): resolve the RETURNED `(name, revision)` in the
    registry, then verify both `content_hash` AND step-count == stored `plan_length`;
    if absent or mismatched → durably defer (`revision_unavailable`), never
    substitute. The procedure does NOT (and cannot) consult the in-process registry.
- **`plan_length` stays a DURABLE projection (not derived from the registry).**
  It is a stored column, set + validated against the resolved IR's step count at
  creation (under the `content_hash` pin), and RETURNED on every replay. So
  `operation_request` / `operation_settle` AND terminal replay read finality from
  STORAGE — durable transition validation and result replay never depend on registry
  availability.

## Concrete implementation plan (the four starting sub-steps)
**A. Durable script revision identity + registry lookup.**
- Define the `ScriptIR` type (ordered operations + bindings; today's plan/ops) and
  a `ScriptRegistry` interface: `resolve(name, revision) -> Optional<ScriptIR>` +
  `active_revision(name)` + each entry's `content_hash`.
- Milestone-1 implementation: an **in-process immutable registry** built at startup.
- **`content_hash` is a collision-resistant, versioned digest** — it is the immutable
  identity enforcing "never substitute," so it must be cryptographically strong, NOT
  the `_plan_hash` MD5/UUID-v3 construction. Contract:
  - a CANONICAL encoding of the revision IR (deterministic field order, length-prefixed
    fields — the existing `_lp` framing — covering each step's operation, input,
    resolved schema_version, participant id, and compensation op/version/participant);
  - `content_hash = scheme_tag (1 byte) ‖ SHA-256(canonical_encoding) (32 bytes)` —
    a SHA-256-class digest with an explicit, versioned leading scheme byte so the
    algorithm can evolve without ambiguity. EXACT representation: a fixed **33-byte**
    value, stored as `tb_mf_workflow_plan.content_hash VARBINARY(33)` (the host passes
    33 raw bytes; fixtures encode 66 hex chars), replacing the old `VARBINARY(16)`
    `plan_hash`. The proc may `CHECK (LENGTH(content_hash) = 33)`.
  - This is DISTINCT from the per-operation `input_hash` (UUID-v3 / `nameUUIDFromBytes`),
    which keys operation idempotency at the participant and is not a security identity.
- Fold the pin: `tb_mf_workflow_plan.plan_hash` → `content_hash` (keep
  `plan_length`). `create_planned` compares SUPPLIED-vs-STORED identity only and
  RETURNS the FULL stored pin (incl. `plan_length`) on replay; the **runner** does
  the registry resolution + `content_hash`/length verification.

**B. Deployment / activation without mutating existing revisions.**
- A registry **source** (milestone-1: a config block listing revisions
  `{name, revision, ir}` + an `active` map) loaded into the immutable registry at
  startup; build into a STAGING set and swap atomically; reject the whole reload
  if any revision is invalid or identities conflict. Existing revisions are
  byte-for-byte retained. Activation = move the `active` pointer; rollback = move
  it back. (The §10 production form is a filesystem manifest + SIGHUP atomic
  reload — see Open questions for the milestone-1 trigger.)

**C. Runner loads the pinned IR — STATE-SENSITIVE ordering (durable pin → create only
if absent → claim/inspect → resolve).** Read the DURABLE pin FIRST so an existing
workflow never touches the active registry; the active revision is read ONLY when
creating an absent workflow. IR resolution happens only AFTER a successful claim
(a `revision_unavailable` defer needs the fence a claim grants; terminal replay
needs none).
1. **`pin = plan_get(workflow_id)`** — a STORAGE read of the durable pinned
   `(name, revision, content_hash, plan_length)`.
   - **Some(pin)** (EXISTING workflow): use it directly — no active-registry read, no
     create. (A terminal or resuming workflow whose script has no *active* entry still
     proceeds: terminal replays from durable state; a claimable resume resolves its
     *pinned* revision, deferring if that revision is unavailable.)
   - **None** (fresh submission): read the registry's active
     `(name, active_rev, active_hash, active_plan_length)` and
     `create_planned(workflow_id, active…)` → returns the FULL committed pin. The
     create RESOLVES THE RACE: a concurrent creator that won returns its `Exists`
     winning pin; `plan_conflict` only on a contradictory immutable identity. Use the
     returned pin.
2. claim:
   - **Claimed** (forward / reversing): NOW resolve `pin.(name, revision)` in the
     registry, verifying `content_hash` + step-count == `pin.plan_length`. Absent /
     mismatched → `_defer_dispatch(revision_unavailable)` (the claim gave us a fence),
     never substitute. Otherwise dispatch / unwind the resolved IR (reversal binds
     compensation from the PINNED revision).
   - **NotClaimable** → inspect:
     - **terminal** → replay from DURABLE state using `pin.plan_length` for the final
       operation seq (`operation_result`); no IR / participant / registry needed.
     - **leased** → report active; **deferred / not-due** → report.

**D. Restart + rollback tests across registry updates.**
- Pinned-to-N workflow resumes correctly after N+1 is activated (keeps N).
- **Rollback proves the active pointer moved** (not just that a pinned workflow is
  stable): activate N+1, create W1 (pins N+1); roll back to N; then a NEW workflow W2
  **pins N** (active moved back) WHILE the existing W1 **stays N+1** (pins are
  immutable across rollback). Both halves are required — a pinned-N workflow resuming
  under N proves nothing about activation.
- A workflow whose pinned revision is ABSENT durably defers AFTER claim — never runs
  a different revision (the core safety property).
- Terminal replay of a multi-op workflow whose pinned revision is ABSENT still
  returns the final stored result (durable `plan_length`, no IR).
- A `(name, revision)` offered at a DIFFERENT `content_hash` (or length) →
  conflict/defer.
- All existing reversal / plan-pinning / recovery tests stay green (machinery
  reused, not rewritten).

## Files likely affected
- Runner: script constants → registry resolution; `_build_plan` →
  `registry.resolve`; a NEW per-revision `content_hash` (canonical encoding +
  versioned SHA-256, separate from `_plan_hash`/`_input_hash`); a `plan_get` read
  first; the durable-pin → create-if-absent → claim → resolve ordering;
  `revision_unavailable` defer.
- Host + proc: a `plan_get(workflow_id)` read; `create_planned` /
  `tb_mf_workflow_plan` (`plan_hash VARBINARY(16)` → `content_hash VARBINARY(33)` =
  1 scheme byte + 32-byte SHA-256) RETURNS the full pin incl. `plan_length` on
  Created + Exists.
- Integration config + fixtures: move `plan`/`operations` into a `scripts`
  registry shape; pins carry `content_hash` + `plan_length`.

## Verification criteria
- Full root `just test` green; new restart/rollback/revision-unavailable/
  content-mismatch integration cases; SP coverage for the evolved pin.
- The existing sub-step A–D reversal/compensation suite unchanged and green.

## What is explicitly reused (not rebuilt)
The runner forward + reverse loops, `operation_request`/`operation_settle`
(incl. plan-aware ordering + finality), the generic dispatcher + recovery, and the
whole reversal/compensation machinery. Only the **source of the IR** changes:
mutable config → immutable versioned registry, pinned per workflow.

## Resolved (were open; settled by review)
- **Fresh vs resume** — one `create_planned` command returning the FULL stored pin
  `(name, revision, content_hash, plan_length)` on replay, so resume never conflicts
  on a moved active revision and has the stored length without the registry. (Distinct
  submit/resume commands were the alternative; the return-stored-pin form is simpler.)
- **Ordering** — durable `plan_get` FIRST → create only if absent (race-safe via the
  returned winning pin) → claim/inspect → resolve. An existing workflow never reads
  the active registry; IR resolution is post-claim (so a `revision_unavailable` defer
  holds a fence); terminal replay reads durable state (`plan_length` +
  `operation_result`) without the IR.
- **`content_hash` algorithm** — canonical encoding + versioned SHA-256-class digest
  (collision-resistant immutable identity), NOT the MD5/UUID-v3 `_plan_hash`.
- **Activation consistency** — process-local active; the durable creation decides the
  pin; deployment must carry every referenced revision. No fleet-wide atomic pointer
  in milestone 1.
- **`plan_length`** — durable stored column, validated against the IR step count at
  creation and RETURNED on every replay; transition procs + terminal replay never read
  the registry.

## Open questions / blockers
- Milestone-1 registry trigger: load-at-startup only, or a SIGHUP-style staged
  per-process reload now (the §10 production form)? Filesystem manifest vs in-config
  revision list for the hand-built IR.

## Out of scope / deferred
- **The parser** (explicit): deferred until execution + storage contracts are
  stable. JSON IR serialization stays deferred (§10: JSON is not the language).
- **`blocked_resolution` administration** (authorized retry / resolve /
  accept-exception OUT of blocked) — a separate follow-up milestone from the
  reversal slice.
- **storage-portability audit** — planned after ScriptRegistry, before the parser
  (see `work/storage-portability/`).

## Relevant roadmap
§7 sequence: dispatcher ✓ → reversal ✓ (A–D, `a62b34d`) → **manual portable IR via
ScriptRegistry (this effort)** → storage-portability audit → parser.
