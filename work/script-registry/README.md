# script-registry  (roadmap §7 step: manual portable IR → ScriptRegistry)

## Short-term objective
Replace the config-embedded forward plan with **immutable, validated IR resolved by
EXACT MATCH on `(plan_name, plan_version, content_hash)`** through a `ScriptRegistry`
abstraction. Preserve the existing runner loop, plan pinning, dispatch, recovery, and
reversal machinery unchanged. **Parser stays deferred** — the IR remains hand-built
until the execution + storage contracts are stable.

## ✅ v1 scope (DECIDED — read first)
The retention model is **settled for v1: a strict, simple, exact-match, single-
generation model.** No automated compatibility routing is built; breaking evolution is
an OPERATIONAL convention (publish a new plan NAME), not scheduler machinery.

- **Every plan has an immutable semantic version `major.minor.patch`** (validated). The
  three identifiers stay distinct:
  - `plan_name` (a.k.a. `script_name`) — the workflow contract/generation.
  - `major.minor.patch` — an immutable release of that plan.
  - `content_hash` — proves the named version's compiled IR has not changed.
- **A process loads ONE configured plan generation.** It may execute ONLY workflows
  whose pinned `(plan_name, plan_version, content_hash)` EXACTLY match an available
  plan. Semver does NOT yet authorize substituting another version — `major/minor/patch`
  is the *future* compatibility vocabulary (major=boundary, minor=backward-compatible
  capability, patch=compatible correction); v1 selection is exact-match.
- **Persist the pin `(script_name, plan_version, content_hash, plan_length)`** per
  workflow. An existing version is NEVER republished with different content.
- **Pinned plan unavailable after reload/deploy → recoverably STALLED.** Persist a
  deduplicated operational `revision_unavailable` condition (the dedup'd
  `operation_dispatch_deferred` audit event), CLEAR the lease, leave it nonterminal and
  claimable. It MUST NOT fail, reverse, enter `blocked_resolution`, or run under another
  version. Restoring a compatible plan lets it continue automatically.
- **Observability:** a read-only inspection SP (`sp_mf_plan_stalled`) lists workflows
  stalled on `revision_unavailable` with their pin + state/direction + timing + reason.
  No Microflows API / metrics / dashboard yet.
- **Operational breaking-change convention (no code):** publish `checkout-v2@2.0.0`
  alongside `checkout-v1@1.4.2`; existing workflows stay pinned to v1, new submissions
  select v2; once `sp_mf_plan_stalled`/inspection shows no nonterminal v1 workflows,
  operations remove v1. The registry is keyed by `(name, version)`, so multiple NAMED
  plans coexist naturally; removing a still-referenced plan leaves those workflows
  recoverably stalled (not failed).

**Explicitly NOT built in v1** (documented direction only): semver-range/compatibility
routing, active-version pointers, fleet capability discovery, multi-version retention
of the SAME name, historical GC, migrations, multi-version scheduling, and the
quiesce / graceful-shutdown / `pending_restart` / 503-admission lifecycle (the
intended reload/shutdown direction — stop admission/claims, 503 new submissions,
release/suspend owned work without changing semantics — but the runner is a one-shot
CLI today with no admission surface, so it is deferred).

Sections below describing multi-revision activation / rollback / N+1 coexistence are
**forward-looking** beyond v1. The landed v1 work is the semver pin + exact-match +
recoverable-stall + inspection SP, plus the three named static-review fixes
(storage-first ordering, creation-race winner adoption, reversal-from-pinned-plan).
Canonical (key-ordered) input hashing remains a SEPARATE open fix (not in the v1 ask).

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
> **v1 status:** Sub-step A LANDED and evolved to the decided v1 model — the pinned
> identifier is now the immutable semver `plan_version` (not an integer revision), the
> pin is `(script_name, plan_version, content_hash, plan_length)`, selection is
> exact-match, and static-review items 1–3 are fixed. Sub-steps B/D below remain
> forward-looking (multi-revision activation/rollback is beyond v1; distinct named plans
> cover v1 rollouts). Read "✅ v1 scope" up top first.

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

**B. Deployment / activation without mutating existing revisions.** — ⏸ PAUSED
(gated on the retention decision; see Milestone-1 scope). The notes below are
forward-looking.
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

**D. Restart + rollback tests across registry updates.** — ⏸ PAUSED (needs
multi-revision retention; gated on the retention decision). Forward-looking:
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

## Static review findings — sub-step A
The three findings the user named are FIXED in v1. The fourth (not in the v1 ask) stays
open.
- **Item 1 (High) — storage-first mode + pin. ✅ FIXED.** `_run` now reads `plan_get`
  FIRST; the durable pin decides planned-vs-legacy mode. The config plan is consulted
  only to create a fresh workflow (pin NotFound) or pick the legacy path; `_validate_plan`
  moved to the fresh path; `_registry_build` is graceful (no plan → empty registry →
  `revision_unavailable`, never a fatal throw). So an existing planned workflow whose
  pinned plan is absent/changed in this generation defers, never misroutes to legacy.
- **Item 2 (High) — creation-race winner adoption. ✅ FIXED.** For the SAME plan name,
  `create_planned` Exists RETURNS the winning durable pin `(script_name, plan_version,
  content_hash, plan_length)` and never conflicts on a differing version/hash/length; the
  caller adopts the winner and exact-match-resolves it locally (a winner from another
  generation → recoverable `revision_unavailable`). `plan_conflict` is reserved for a
  contradictory identity: a DIFFERENT stored plan name (id collision — refined in round 2)
  or a legacy non-plan workflow.
- **Item 3 (High) — reversal-from-pinned-plan. ✅ FIXED.** `_run_planned` resolves +
  verifies the pinned `(name, version)` + content_hash BEFORE choosing a direction; a
  reversing workflow whose pinned plan is unavailable defers (`revision_unavailable`)
  rather than unwinding under a mismatched plan. Under v1's single verified generation,
  the verified cfg IS the pinned plan, so cfg-bound compensation is then provably the
  pinned one.
- **Item 4 (Medium) — canonical input hashing. ✅ FIXED.** `_plan_canonical` now hashes
  the recursively key-ordered compact input (`_canonical_json`, same lex ordering as
  `_input_hash`), so JSON objects differing only in key ORDER produce the same
  `content_hash` (no false `revision_unavailable`). The wf21 seed (multi-key input) was
  recomputed; single-key seeds are unchanged.

### Round 2 static-review fixes (all ✅)
- **Configurable plan names can create workflows.** `_run_planned` resolves the active
  generation by the CONFIGURED plan name (`_config_str(cfg, "script_name", …)`), not the
  `SCRIPT_NAME` constant — so e.g. `checkout-v1` no longer returns `no_active_revision`.
  Integration: `forward_named_plan_completes`.
- **Id collision across plan NAMES → `plan_conflict`.** `create_planned` now conflicts when
  the stored plan name differs from the submitted one (a different contract for the same
  workflow_id), while still adopting the winner for same-name/different-version races. So
  `checkout-v2` cannot silently adopt an existing `billing-v1` workflow. SP:
  `create_planned_name_conflict`.
- **Terminal replay is registry-CONFIG-independent.** `_validate_registry` and
  `_registry_build` are deferred PAST the claim; a NotClaimable (terminal) workflow reports
  from durable state before any registry is built or validated, so a completed workflow
  replays its result even when current participant/operation config is absent or malformed.
  Integration: `terminal_replay_registry_config_independent`.

### Round 3 static-review fixes (all ✅)
- **No lease leak on post-claim config failure.** The deferred (post-claim) registry
  build/validate is wrapped: on a throw the runner durably defers `revision_unavailable`
  (lease released, recoverable) instead of exiting still holding the lease. Integration:
  `forward_malformed_registry_defers_no_lease_leak`.
- **`create_planned` race-safety via the CALLER-OWNED transaction.** The workflow PK INSERT
  holds its lock until the caller COMMITs, so a racing creator blocks until the winner's
  full pin is durable and then reads it (never a partial / spurious-conflict read). The proc
  does NOT manage its own transaction — uniform with every other SP (`_call_sp_doc` +
  `rpc.commit`); a round-3 attempt to self-manage (`START TRANSACTION`/`COMMIT`/EXIT handler)
  was reverted because it implicitly commits the caller's open tx and seizes publication.
  SP: `create_planned_concurrent_race_atomic` — two connections, `autocommit=False` +
  COMMIT-after-call (the host's contract).
- **`sp_mf_plan_stalled` excludes reclaimed retries.** Added `lease_owner IS NULL` so only
  genuinely lease-cleared stalls are reported (a reclaimed, actively-retrying workflow drops
  off even if its latest event is still the deferral).
- **Test totals: derived display + completeness guard.** The sp_operation + integration
  harnesses derive `passed`/`total` from the checks that ran (honest N/N), AND keep an
  `EXPECTED_CHECKS` manifest that FAILS the run if the ran-count drifts — so a deleted or
  bypassed check can't hide behind a self-reported N/N. Work notes carry no counts.

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
- **Retention model — SETTLED for v1.** Exact-match, single-generation, strict. Breaking
  changes take a new plan NAME (operational convention); compatible fixes may take a new
  semver but started workflows stay pinned to their exact version. The future
  compatibility-routing question (semver ranges authorizing substitution) is explicitly
  out of v1.
- Registry trigger (future, beyond v1): load-at-startup vs a quiesce-then-staged reload;
  filesystem manifest vs in-config plan list.

## Out of scope / deferred (beyond v1)
- **Semver-range / compatibility routing, active-version pointers, fleet capability
  discovery** — future; v1 is exact-match only.
- **Multi-version retention of the SAME name, historical GC, migrations, multi-version
  scheduling** — not built; coexistence of distinct NAMED plans covers v1 rollouts.
- **Quiesce / graceful-shutdown / `pending_restart` / 503 admission** — the intended
  reload/shutdown direction, documented only; the runner is a one-shot CLI with no
  admission surface today.
- **The parser** (explicit): deferred until execution + storage contracts are stable.
- **`blocked_resolution` administration** — separate follow-up from the reversal slice.
- **storage-portability audit** — after ScriptRegistry, before the parser.

## Relevant roadmap
§7 sequence: dispatcher ✓ → reversal ✓ (A–D, `a62b34d`) → **manual portable IR via
ScriptRegistry (this effort)** → storage-portability audit → parser.
