# Release — workflow composition MVP (singular 0.8.0 · microflows 0.6.0 · uflowsd 0.5.0)

> **Status: CERTIFIED.** Certified by `build-orchestrator` run
> `20260703-174026-drift-lang-5c6e03f` — `drift-workflows @ 7fb7f98`, both `normal` and `debug` lanes
> PASS, submitted alongside `drift-lang@5c6e03f`, `drift-mariadb-client@92fcc3e`,
> `drift-net-tls@b9550de`, `drift-web@a152b48`. `drift/manifest.json`, `drift/lock.json`, and
> `drift/*.author-claim` all reflect this certified state.
>
> **This is a separate, later body of work from the already-certified
> `work/uflowsd/RELEASE_ANNOUNCEMENT_DRAFT.md` (microflows 0.5.0 · uflowsd 0.3.0, Status:
> CERTIFIED).** That announcement is historical — it predates workflow composition entirely and
> describes a different feature set (case-[12] pending-redispatch, the #2 reconcile budget, #5
> node-address ids, result-conditional branching, the `failed`/`compensated` terminal). Composition
> landed in later commits and is not part of that cert. See "Version audit" below for how the two
> relate.

## Version audit

| Artifact | Was (last certified) | Now (`drift/manifest.json`) | Status |
|---|---|---|---|
| `singular` (package) | 0.7.0 | **0.8.0** | bumped after this draft — a real source fix (`RpcCommitError` compatibility, see "Post-draft fixes") |
| `microflows` (package) | 0.5.0 | **0.6.0** | as proposed in this draft |
| `uflowsd` (app) | 0.3.0 | **0.5.0** | bumped once more past this draft's proposed 0.4.0 — a second real source fix (`pub` entry points, see "Post-draft fixes") |

`mfrunner` (one-shot CLI) and `microflows-participant-stub` remain component-local dev artifacts
(`0.0.0`) — unchanged convention, not Drift release artifacts. `mfinspect` (new, this body of work)
is a standalone Python tool with its own `pyproject.toml` version (currently `0.1.0`), packaged as a
self-contained zipapp the same way `mariachi` is — it is **not** a Drift package/app and is **not**
listed in `drift/manifest.json`'s `artifacts`, matching the existing convention for that class of
tool.

**Confirmed sealed/certified history:** `work/uflowsd/RELEASE_ANNOUNCEMENT_DRAFT.md` explicitly
states `**Status: CERTIFIED.**` and its content (headline features, breaking-changes list,
verification counts — 208 checks, driftc 0.33.64) matches exactly `microflows/CHANGELOG.md`'s
current top entry, which describes case-[12] pending-redispatch and predecessor work. Git history
confirms the announcement's only edit since that content was written is a one-line naming fix
(`microflows-runner` → `mfrunner`), unrelated to composition — composition (`NeedCall` dispatch,
then reverse-child compensation) landed in later, separate commits, entirely absent from that
document. **Note for the cert team:** `microflows/CHANGELOG.md`'s top entry is still labeled
"unreleased" even though its own announcement says "CERTIFIED" — that pre-existing label
inconsistency is unrelated to composition and is flagged here for the cert team's own judgment, not
silently resolved by this pass.

**Why 0.6.0/0.4.0 (this draft's original proposal), not reusing 0.5.0/0.3.0:** the 0.5.0/0.3.0 numbers
are already sealed against a specific, different, already-verified feature set and verification count.
Composition adds a real new package surface (`host.drift`:
`checkpoint_reverse_child_reopen`/`checkpoint_reverse_child_settle` methods + outcome types) and a real
new schema/runtime footprint (below) on top of that sealed content — per this repo's own pre-1.0
convention (`CHANGELOG.md`'s header note: "breaking changes are expected and arrive in minor bumps"),
that is a new minor bump, not a re-use of an already-sealed one.

## Post-draft fixes (why the final versions moved past this draft's original proposal)

Two real, unrelated compatibility breaks surfaced during cert submission after this draft was first
written, each requiring its own version bump:

- **`RpcCommitError` compatibility (`singular` 0.7.0 → 0.8.0).** Upstream `drift-mariadb-client`
  redesigned `RpcCommitError` from a `tag`/`message` `pub error` into a plain struct — `kind`
  (`AmbiguousWrite`/`NotSent`/`ServerRejected`) plus `cause_tag` for diagnostics only, documented as
  "consumers branch on `kind`". Both `microflows/.../host.drift` and `singular/.../gateway.drift` still
  read the old `.tag` field. Fixed in both: `ServerRejected` → `BackendRejected` (non-retriable,
  unchanged), `AmbiguousWrite`/`NotSent` → `BackendUnavailable` (retriable — collapsing all three into
  a hard rejection, as an interim compat-only pass first did, was flagged in review as semantically
  wrong and corrected). A related break in `live_reversal_test.drift`'s fixture-seeding helper
  (`RpcCommitError` no longer being a `pub error` broke `.or_throw()`) was fixed with an explicit
  `match` + rethrow as `rpc.RpcError`.
- **`pub` entry points required (`uflowsd` 0.4.0 → 0.5.0).** driftc >= 0.33.67 rejects any `--entry`
  target not declared `pub`. `microflows.runner::main`/`::service_main` needed `pub fn`, and so did
  every unit/e2e/stress/perf test's `fn main` across both `singular` and `microflows` (14 files) — the
  emitters' own `is_test_entry()` check was updated to recognize an optional `pub` prefix, matching
  `drift-mariadb-client`'s own already-updated pattern. Also required refreshing three stale dependency
  locks (`drift/lock.json` and the `microflows/runner`/`microflows/participant-stub` component-local
  ones) pinned to `net-tls`/`web-client`/`web-jwt`/`web-rest` versions no longer present in the current
  package pool.

Both were caught and fixed via direct iteration against the cert pipeline's own toolchain and package
pool (not a locally-cached one) after real cert-run rejections — see the two `build-orchestrator` run
logs referenced in "Certification" below for the full before/after.

## What's in this draft

### Workflow composition MVP

- **Typed workflow args/returns across a call.** A step assembles the child's args from the parent's
  own durable arguments/results; the child's declared `return` type is bound to the call's result
  binder, read downstream exactly like a participant result (`result r.field`).
- **`call child@<plan_version> { ... }` — one workflow call per step, async but awaited.** Occupies
  one durable step with the same seq/settle discipline as a participant operation; the child's own
  workflow id is derived deterministically (`H(parent workflow_id, parent content_hash, call node
  id)`), so a resumed drive re-derives the identical child id and never creates a second one, and two
  different parent instances of the same script never collide on the same call node.
- **The child is an ordinary async workflow, awaited like a participant call.** `call_submit` is
  idempotent on every pass (fresh or resumed); the parent awaits the child's terminal outcome via a
  pure read (`call_inspect`) — the same read both the forward await and the reverse-side await use.
- **No blocked cascade.** A child that is non-terminal — forward, reversing, or stuck in
  `blocked_resolution` — never puts the parent into a blocked state; the parent simply stays
  `pending` (carrying the child's id) and polls again. This holds symmetrically on both the forward
  and reverse side.
- **Reverse-child compensation.** If the parent itself reverses, its call checkpoint asks the
  (already-completed) child to compensate *itself* — recursively, through arbitrarily nested call
  chains, via the exact same generic reversal machinery every workflow already has. The parent never
  enumerates or reaches into a child's own checkpoints; it only asks "are you done undoing yourself
  yet," and settles once the child reports back compensated. Two new stored procedures:
  `sp_mf_checkpoint_reverse_child_reopen` (fenced, idempotent `completed(4)→reversing(2)` reopen,
  parent+child atomic) and `sp_mf_checkpoint_reverse_child_settle` (independently re-verifies the
  child actually reached `reversed(5)`/`resolved_exception(6)` before flipping the parent's own
  checkpoint — never trusts a prior read). Replaces the earlier `sp_mf_checkpoint_reverse_noop`
  (retired, not shipped in any prior cert).
- **`mfinspect`** — a read-only recursive JSON dump of a workflow's full call/event/checkpoint tree
  (`microflows/tools/mfinspect/`, packaged as a self-contained zipapp like `mariachi`). Built ahead of
  reverse-child compensation specifically because the reversal-across-a-tree integration work needed
  it immediately; every T1/settle event carries `child_workflow_id`
  (and, on the child's own reopen event, `parent_workflow_id` + the triggering `operation_seq`), so a
  durable event, a service log line, and an `mfinspect` dump can all be joined by the same
  identifiers.

### Explicitly excluded from this MVP (deferred, not forgotten)

- **No fan-out.** `call` is one child per step; no "call N children and gather."
- **No `on failed` / failure-as-data.** A child that terminates without completing (rejected,
  reversed, or failed) always drives the parent's own reversal — there is no way for the parent's
  script to branch on that as a value. (A non-terminal/blocked child does *not* drive reversal; it
  just keeps the parent `pending` — see "No blocked cascade" above.)
- **No stuck-child liveness budget.** An indefinitely non-terminal child (forward or reverse) is not
  bounded by a timeout/budget in this MVP — matching the forward side's own pre-existing lack of one.
- **No separate compensating-workflow mode.** `compensation <wf>@<version>` stays build-rejected,
  unchanged from the original 1a slice — a child compensates via its own ordinary reversal, not a
  distinct authored "compensation script."

## Migration

Two migrations postdate the certified 0.5.0/0.3.0 cut and are **not** mentioned in that
announcement — both are needed for this composition MVP:

- **`0004_workflow_return.sql`** — adds `tb_mf_workflow.workflow_return_json` (the authoritative typed
  workflow return, separate from any per-operation result). No backfill: every pre-existing
  `completed(4)` row's implicit terminal was unit before this feature, so `{}` is the correct
  backfilled value everywhere reachable.
- **`0005_workflow_call.sql`** — adds the `call_kind` discriminator on `tb_mf_operation`, four
  ancestry columns on `tb_mf_workflow` (`parent_workflow_id`/`parent_node_id`/`root_workflow_id`/
  `call_depth`), and the new `tb_mf_call` sidecar table. No backfill: every pre-existing
  `tb_mf_operation` row is, by construction, a participant call (`call_kind` didn't exist before this
  feature), and every pre-existing `tb_mf_workflow` row is top-level (composition didn't exist
  before this feature) — the schema defaults (`call_kind` DEFAULT 1, ancestry columns NULL) are
  exactly correct with zero data changes.

Reverse-child compensation's own two new stored procedures (`sp_mf_checkpoint_reverse_child_reopen`/
`_settle`) add no further table/column changes — stored procedures are re-applied wholesale by the
schema-templating tool on every apply, not incrementally migrated, so no third migration file is
needed for them.

Apply in order: `0001` → `0002` → `0003` (already covered by the 0.5.0 cert) → `0004` → `0005`
(new, this draft). Fresh installs get every column/table from the `schema/*.sql` files directly (no
migration needed).

## Breaking changes

None to the participant `/microflows/v1/…` HTTP contract. The one internal-only removal
(`sp_mf_checkpoint_reverse_noop`, replaced by the two new SPs above) was never part of any prior
cert and has no external caller — not a breaking change for a deployment upgrading from the
certified 0.5.0/0.3.0.

## Verification

- `microflows` component `just test`: unit/e2e 25/25 (base/asan/memcheck), parser fixtures 100/100,
  manifest fixtures 11/11, SP regression 156/156 (operation) + 131/131 (call), runner-level
  `call_integration_test.py` 50/50 (including nested A→B→C cascading compensation and the
  blocked-child no-cascade case).
- `integration/coordinator-singular` suite: 231/231, including the new `ex_composition_*` checks —
  a real `order_fulfillment`/`shipment_booking` example workflow pair proving the full
  submit → child completes → later-step rejection → parent defers (no cascade) → child compensates
  itself → parent settles/compensates cycle through the real `uflowsd` service.
- Root `just test` (singular → microflows → every `integration/<suite>`): fully green, no
  mixed-red, no expected-red items remaining.

Full per-change detail: `work/workflow-composition/PROGRESS.md` (day-to-day status),
`work/workflow-composition/DESIGN.md` (original charter + slice plan),
`work/workflow-composition/1c-design.md` (the compensation transition spec), and
`microflows/doc/microflows_design.md` §16 (as-built summary).

## Certification

**CERTIFIED** by `build-orchestrator` run `20260703-174026-drift-lang-5c6e03f`:

```
Certification Result: CERTIFIED
Submitted commits:
  - drift-lang @ 5c6e03f
  - drift-mariadb-client @ 92fcc3e
  - drift-net-tls @ b9550de
  - drift-web @ a152b48
  - drift-workflows @ 7fb7f98
Result by repo:
  - drift-workflows (normal): PASS
  - drift-workflows (debug): PASS
```

`drift/manifest.json` (`singular` 0.8.0, `microflows` 0.6.0, `uflowsd` 0.5.0), `drift/lock.json`, and
`drift/*.author-claim` all reflect this certified commit. This status was set by `build-orchestrator`'s
own process, not asserted from this repo.
