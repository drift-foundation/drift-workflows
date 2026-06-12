# script-registry — Progress / status

Design + plan live in [README.md](./README.md); this is the at-a-glance status.

## Status: **v1 model LANDED** (immutable semver pin + exact-match + recoverable stall + inspection SP)

The runner loads ONE configured plan generation `(script_name, plan_version)` into an
in-process immutable `ScriptRegistry`. A workflow durably pins
`(script_name, plan_version, content_hash, plan_length)`; `plan_version` is a validated
semantic version `major.minor.patch`. Selection is EXACT-MATCH on (plan_version AND
content_hash) — a mismatch durably defers `revision_unavailable` (lease cleared,
nonterminal, recoverable), never failing, reversing, or substituting. The three named
static-review fixes are in. Quiesce/503 is documented direction only (one-shot CLI).

## What landed (this round)
- **Semver pin.** `tb_mf_workflow_plan.plan_version VARCHAR(32)` (CHECK `major.minor.patch`);
  `create_planned` takes/stores/returns `plan_version` (dropped the int `script_revision`
  arg; tb_mf_workflow.script_revision stays a legacy int = 1); `plan_get` returns it;
  host `PinnedScript.plan_version`; runner registry keyed by `(name, version)` string,
  config-driven (`script_name`/`plan_version`, default `protocol_spike`/`1.0.0`) +
  `_validate_semver`.
- **Exact-match.** `_registry_resolve` matches name+version exactly; caller then verifies
  content_hash + length. No semver ranges.
- **Item 1 — storage-first.** `_run` reads `plan_get` FIRST to decide planned-vs-legacy
  mode + pin; `_validate_plan` only on the fresh path; `_registry_build` graceful.
- **Item 2 — creation-race winner.** `create_planned` Exists returns the WINNING durable
  pin for the SAME plan name (never conflicts on a differing version/hash/length);
  `plan_conflict` for a contradictory identity — a DIFFERENT stored plan name (id collision)
  or a legacy non-plan id. (Name-conflict refined in round 2; see below.)
- **Item 3 — reversal-from-pinned-plan.** `_run_planned` resolves+verifies the pinned
  plan BEFORE branching to forward/reverse, so a reversing workflow never unwinds under a
  mismatched plan.
- **Recoverable stall + observability.** `revision_unavailable` reuses the dedup'd
  `operation_dispatch_deferred` durable condition (lease cleared, forward, claimable). New
  read-only `sp_mf_plan_stalled` lists stalled workflows with pin + state/direction +
  timing + reason.

## Round-2 static-review fixes (this round)
- **Item 4 — canonical input hashing.** `_plan_canonical` hashes recursively key-ordered
  input (`_canonical_json`); equivalent JSON in any key order → same `content_hash`. wf21
  seed recomputed (`0123cb1a…`).
- **Configurable plan names create workflows.** active lookup uses the CONFIGURED
  `script_name`, not the constant (`checkout-v1` works). Integration `forward_named_plan_completes`.
- **Id collision across plan NAMES → `plan_conflict`.** `create_planned` conflicts on a
  differing stored plan name (still adopts winner for same-name version races). SP
  `create_planned_name_conflict`.
- **Terminal replay registry-CONFIG-independent.** `_validate_registry` + `_registry_build`
  deferred past the claim; a terminal workflow replays even with absent/malformed
  participant config. Integration `terminal_replay_registry_config_independent`.

## Round-3 static-review fixes (this round)
- **No lease leak on post-claim config failure.** A claimed workflow whose post-claim
  registry build/validate throws is caught and durably defers `revision_unavailable`
  (lease released, recoverable), never exiting still holding the lease. Integration
  `forward_malformed_registry_defers_no_lease_leak` (wf26).
- **create_planned race-safety via CALLER-OWNED transaction.** (Round-3 self-managed
  transaction was REVERTED — it broke the uniform `_call_sp_doc`/`rpc.commit` contract:
  `START TRANSACTION` implicitly commits the caller's open tx and the internal `COMMIT`
  seizes publication.) The proc stays caller-owned; the workflow PK INSERT holds its lock
  until the caller COMMITs, so a racing creator reads the full winning pin (never a partial
  read / spurious `plan_conflict`). SP `create_planned_concurrent_race_atomic` runs two
  connections with `autocommit=False` + COMMIT-after-call (the host's contract).
- **`sp_mf_plan_stalled` excludes reclaimed retries.** Added `lease_owner IS NULL` — only
  genuinely lease-cleared stalls are listed.
- **Test totals: derived display + completeness guard.** Harness pass/fail counts are
  derived (`passed + failures`) so the displayed N/N is always honest; an `EXPECTED_CHECKS`
  manifest fails the run if the ran-count drifts (a deleted/bypassed check can't hide).

## Sub-step ledger
- [x] **A — semver pin + registry + exact-match + pin-first flow + items 1/2/3.**
- [ ] **B — deployment/activation** — forward-looking (multi-revision; beyond v1).
- [~] **C — pin-first IR load** — DONE as part of A.
- [ ] **D — restart/rollback across revisions** — forward-looking (beyond v1).

## Still open / deferred
- Semver-range routing, active-version pointers, fleet discovery, multi-version retention
  of one name, GC, migrations, and the quiesce/503 lifecycle — documented direction only.

## Verification
Full root `just test` green (singular + microflows e2e + SP regression + integration
coordinator-singular). New coverage: `create_planned` semver pin, sequential +
genuinely-concurrent winner adoption, bad-semver SIGNAL, name-collision `plan_conflict`,
legacy `plan_conflict`, `plan_stalled` lists/excludes (incl. lease-cleared predicate);
integration `forward_plan_version_conflict`, `forward_named_plan_completes`,
`terminal_replay_registry_config_independent`, `forward_malformed_registry_defers_no_lease_leak`.
Pin seeds carry `plan_version=1.0.0`; wf25 (version mismatch) + wf26 (malformed-config
defer) seeds added; wf21 seed recomputed for canonical input hashing. (Test pass/fail
counts are self-derived in the harness output — intentionally not transcribed here.)

## Uncommitted worktree
New: `db/procs/sp_mf_plan_get.sql`, `db/procs/sp_mf_plan_stalled.sql`. Changed:
`tb_mf_workflow_plan.sql` (plan_version), `sp_mf_workflow_create_planned.sql`,
`sp_mf_plan_get.sql`, `host.drift` (PinnedScript/create_planned/plan_get), `runner.drift`
(semver registry + storage-first + reversal-verify), fixtures (plan pins + wf25 seed +
events/workflow), `sp_operation_test.py`, `coordinator-singular/test.py`.
