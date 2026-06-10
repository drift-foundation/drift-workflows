# durable-request-recovery-test

## Short-term objective
Permanently regression-test durable-request recovery: a resumed standalone
worker must reuse the **persisted** operation request, never re-derive it from
CLI input. A workflow with a persisted request, seeded due, is resumed with a
**different/omitted** CLI input and must defer pending again — never raise
`operation_conflict`.

## Current behavior / problem
Recovery was originally only **manually** verified. The harness (now
`integration/coordinator-singular/test.py`) is stdlib-only and could not, by
itself, reach the resume state: the pending deferral parked the workflow with a
future `next_attempt_at`, so an immediate re-run found it not-yet-due. An interim
version forced the state with a `mariadb` CLI shell-out — rejected: we don't want
that dependency in the harness.

## Accepted design decisions
- **Seed declaratively via a Mariachi scenario, not ad-hoc SQL.**
  `microflows/db/scenarios/coordinator-fixtures/*.data.csv` seeds fixed-id rows;
  `mariachi scenario --name coordinator-fixtures` resets `microflows` to a clean
  base and overlays them. The integration suite's `just test` runs this first — a
  clean, repeatable state every run. No `mariadb` shell-out in the harness.
- **`\N` for SQL NULL.** Required Mariachi support (shipped in **1.0.0**). Used
  for `tb_mf_operation.result_json` on `status=1` ('requested') fixtures, which
  the `ck_mf_operation_status_result` CHECK forbids from being non-NULL.
- **Expired lease instead of a released (NULL) lease** on the workflow rows, so
  the only column needing `\N` is `result_json`. An expired lease
  (`lease_expires_at` far past) is claimable all the same.
- **Fixtures seed a matching audit trail** (`tb_mf_workflow_event.data.csv`) so
  `current_event_seq == max(event_seq) == count(events)` — the only inconsistency
  a fixture carries is the one its test intends (e.g. completed-but-unsettled).
- **The integration suite resets Singular too.** The participant keys operations
  on `(operation, operation_id)` only (no service_group), so a fixed fixture
  operation id would otherwise carry stale participant state across runs. A clean
  Singular per run keeps the suite hermetic (verified: two back-to-back runs are
  identical). CSVs are LF (git `diff --check` clean).
- **Distinguish reuse from re-derivation by input divergence.** The seeded
  request carries a `_fault.respond_pending` input. The resume passes a clearly
  different input. Correct recovery reuses the durable request → 202 → defers
  pending (exit 9). Broken recovery re-derives the input hash → `operation_request`
  reports `operation_conflict` (exit 3). Unambiguous.
- **Pending retry decoupled from lease duration.** `_defer_pending` reads the
  clock with `PENDING_RETRY_SECONDS` (policy = 1s), not `LEASE_SECONDS`, storing
  the resulting absolute deadline. `ClockReading.lease_expires_at` was renamed to
  the neutral `deadline`.

## Concrete implementation plan
1. [x] Runner: `operation_request_get` on resume overrides CLI-derived fields.
2. [x] Runner: `PENDING_RETRY_SECONDS`; `_defer_pending` uses it.
3. [x] Host: rename `ClockReading.lease_expires_at` → `deadline`.
4. [x] Mariachi: `\N` NULL marker in CSV loader (shipped 1.0.0, external team).
5. [x] Scenario fixtures; the integration suite applies the scenario.
6. [x] Harness: seeded-fixture cases; `_mdb()` removed.
7. [x] Promote the cross-component harness to `integration/coordinator-singular/`;
   root `just test` aggregates it (component gates own their isolated tests).

## Files likely affected
- `microflows/runner/src/runner.drift` — recovery load, `PENDING_RETRY_SECONDS`.
- `microflows/packages/microflows/src/host.drift` — `deadline` rename.
- `microflows/packages/microflows/tests/e2e/live_lease_test.drift` — field rename.
- `microflows/db/scenarios/coordinator-fixtures/{tb_mf_workflow,tb_mf_operation,tb_mf_workflow_event}.data.csv` — fixtures + audit trail (microflows-owned schema asset).
- `integration/coordinator-singular/{test.py,justfile,README.md,tools/emit_test_plan.py}` — cross-component orchestration; compiles both apps from source (no deploy), resets both schemas, seeds fixtures, runs the harness.
- `justfile` (root) — aggregate `test`/`perf`/`stress` with component→integration ordering and suite discovery.
- `microflows/justfile` — `test-slice` removed; SP regression folded into `just test`; schema provisioned BEFORE the e2e lane; test-time `deploy` removed (integration compiles from source).
- `singular/justfile`, `singular/drift/justfile` — `test` provisions the schema first; `db-load-schema` also loads `singular_malformed`; test-time `deploy-dev` removed.

## Verification criteria
```
export MDB_ROOT_PWD=...
just test-integration   # builds both binaries, resets both schemas, seeds, runs: expect 12/12
just test               # full repo aggregate (singular -> microflows -> integration)
```
Recovery is proven when resuming WF_RECOVERY with a different input returns
`{"workflow":"pending"}` exit 9 — never `operation_conflict`.

## Current status and next action
**Done.** Implemented and green (`just test-integration` → 12/12; microflows
`just test-sp` → 28/28). Mariachi 1.0.0 `\N` support landed; harness has no
`mariadb` dependency; cross-component test promoted to `integration/`. No further
action; folder kept as a record until committed.

## Open questions / blockers
- None. (The earlier `mariadb`-dependency concern is resolved by Mariachi 1.0.0
  `\N` + scenario seeding. Requires Mariachi >= 1.0.0 — noted in the integration
  suite recipe comment.)

## Relevant review findings
- Round 2, finding 1 (Medium): `rejection_not_repeating` strengthened — exact
  blocked response + unchanged participant request count.
- Round 2, finding 2 (Medium): durable-request recovery now a permanent
  regression (`durable_request_recovery`), seeded declaratively.
- Round 2, finding 3 (Medium): both completed-without-result branches covered
  (`completed_operation_unsettled`, `completed_without_operation`).
- Round 2, design note: deferrals are absolute DB timestamps, decoupled from
  lease duration (`PENDING_RETRY_SECONDS` + `deadline` rename).
- Round 3, finding 1 (Medium): hermeticity — the integration suite resets
  Singular so the fixed fixture operation id can't reuse stale participant state.
- Round 3, finding 2 (Medium): fixtures seed matching `tb_mf_workflow_event`
  history; `current_event_seq` no longer dangles.
- Round 3, finding 3 (Low): fixture CSVs normalized to LF.
- Round 4: cross-component suite promoted to `integration/coordinator-singular/`;
  component assets stay local; root `just test` aggregates components then
  integration suites (ordered, fail-fast, explicit no-op perf/stress gates).
- Round 5, finding 1 (High): the integration suite builds/deploys BOTH libs from
  current source (`singular just deploy-dev` + `microflows just deploy`) before
  the apps — a clean checkout works; stale packages can't mask a regression.
- Round 5, finding 2 (Medium): each component `test` provisions its own schema
  first, so root `just test` doesn't depend on prior DB state.
- Round 6, finding 1 (High): integration COMPILES both apps from current library
  source (driftc + `--package-root` for external deps only) via an emitter +
  shared executor — no signing keys, no author-claim mutation, no deployed
  packages. Verified: libs removed + keys unset + author-claim mtime unchanged,
  full `just test` exit 0.
- Round 6, finding 2 (Medium): singular `db-load-schema` also loads the
  `singular_malformed` fixture schema, so `malformed_backend_test` passes on a
  genuinely clean DB.
- Round 7, finding 1 (High): every destructive reset / DB-backed step (component
  `db-load-schema` + `test-sp`, the integration reset+harness) is serialized on
  the shared host-global lock `serial-mariadb-mdb114-a` via `flocker` — the same
  key the executor uses for its serial DB jobs — so a reset can't overlap another
  gate's DB use. Integration holds it across reset+harness in one acquisition.
- Round 7, finding 2 (Low): the integration emitter derives each app's + library's
  source closure from its manifest `modules` (mirroring drift-web), so a new/
  nested module is picked up without editing the emitter.
- Round 8, finding 1 (High): DB serialization fails CLOSED — flocker is required
  (ships with the toolchain); if absent, every reset/DB recipe hard-errors instead
  of running unserialized.
- Round 8, finding 2 (Medium): each component `test` holds the shared DB lock
  across its COMPLETE setup+DB-test phase in one acquisition (no gap for another
  checkout to reset/migrate the schema mid-test).
- Round 9, finding 1 (Medium): lock ownership is no longer trusted from ambient
  env vars (`DRIFT_DB_LOCK_HELD`/`DRIFT_DB_GROUP` removed). PUBLIC recipes
  (`test`/`db-load-schema`/`test-sp`) ALWAYS acquire the shared lock and delegate
  to PRIVATE work recipes (`_test-locked`/`_db-load-schema`/`_test-sp`); the
  non-shared executor key flows as a CONTROLLED `--db-group` CLI arg passed only
  by the private locked recipe (default = shared key for direct runs). Verified:
  `DRIFT_DB_LOCK_HELD=1` no longer bypasses the lock, and `DRIFT_DB_GROUP` in the
  environment is ignored.
