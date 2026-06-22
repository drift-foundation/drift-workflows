# Certification-Pool Readiness — Progress

## Status

**Plan revised — single-entry model (cert-team review folded in).** No repo-side implementation landed
yet. The plan's orchestrator-entry/package-root model was CORRECTED (see below); ready to build the gates.

## Done

- Read the build-orchestrator contract + audited the three certified package repos (drift-web,
  drift-mariadb-client, drift-net-tls) for `test`/`stress`/`perf`/`stage_packages`.
- Wrote + revised the plan (`README.md`).
- Per-component lockfiles (singular/drift, microflows, runner, participant-stub) already regenerated via
  `drift prepare` (truthful sha, abi18 family); singular re-signed, microflows author-claim re-minted.
  Gate green at 165/165 on 0.33.53/abi18.

## CORRECTION — orchestrator entry/package-root model (cert-team review)

The orchestrator clones `repo.path` as a **Git root** (`git clone --no-checkout <path>` →
`git checkout <sha>`) and expects `drift/manifest.json` **at the checkout root**. `singular/drift` and
`microflows` are **subdirs of the one drift-workflows Git repo**, so per-subdir entries CANNOT
materialize as checkouts. **Fix:** ONE `drift-workflows` `package_repo` entry, staging both libraries
from a NEW **top-level `drift/manifest.json`** (the drift-web/mariadb-client convention — multiple
artifacts, one manifest, one deploy). They stay two individually-versioned artifacts/author-claims.
Alternatives (monorepo support in the orchestrator; or split into two Git repos) are flagged to the cert
team — both touch their config / repo structure, so not assumed.

## Decisions (locked — see README "Confirmed decisions")

1. Reshaped `singular` → **0.5.0** (new version, not 0.4.1). The reshape (UtcTimestamp / WorkLease /
   EventTimeConflict / InvalidLeaseExpiry) is a **separate sub-step that blocks official singular
   submission**; needs the owners' spec; NOT implemented in this alignment pass.
2. microflows cert **`test` = FULL e2e** coordinator↔singular integration (165 checks, incl. C21/C22).
3. **Two distinct packages** (singular, microflows) — individually packaged, never bundled.
4. **stress/perf = bounded but REAL**: a pass must mean concurrency + throughput are OK for real use;
   **perf carries persisted, committed baselines + regression gating** (catch cascading slowdowns).
5. microflows does **NOT** depend on singular (coordinator uses MariaDB directly; only the test stub uses
   singular, compiled from source). singular ⟂ microflows in the dep graph → can land in parallel.

## Implementation checklist (API-agnostic)

- [x] **singular `just stress`** — lease-contention/idempotent-replay scenario
      (`packages/singular/tests/stress/lease_contention_stress.drift`: 25 rounds × 16 racing workers,
      asserts exactly-one-winner + observer convergence each round), wired via `emit_test_plan.py stress`
      + the component/package justfiles (DB-lock + schema + serial DB group). **GREEN** (exit 0).
- [ ] singular `just perf` (acquire→settle counts vs **committed baseline**).
- [ ] microflows real `just stress` + `just perf` + committed baselines (replace stubs).
- [ ] **NEW top-level `drift/manifest.json` (+ author-claims, lock, trust) at the repo root** declaring
      `singular` + `microflows`; `drift prepare` it; bare `drift deploy --dest <tmp>` from the ROOT stages
      both.
- [ ] Confirm root `just test` runs the full integration under staged toolchain/libs (DB + HTTP).
- [ ] DB env contract explicit; resolve **Mariachi** dependency (microflows schema load shells to
      ../../mariachi — make self-contained or declare it) → orch-team message.
- [ ] Local dry-run of root test/stress/perf/deploy under staged roots.
- [ ] Draft orch-team message (the single `drift-workflows` entry, dep+affects edges, monorepo/split
      call-out, env+Mariachi contract, version pins, 0.5.0 sequencing).

## Next action

Implement singular/drift `stress` + `perf` first (mirror certified drift-mariadb-client's perf_baseline +
DB-serialized stress harness — closest analog), then microflows, then the top-level cert manifest. Track
boxes above.

## Notes

- Do NOT edit build-orchestrator; craft a message for the orch team instead.
- Do NOT treat local `build/dist/lib` as cert output; do NOT hand-edit locks/claims.
- DB tests need MariaDB 127.0.0.1:34114 + MDB_ROOT_PWD + flocker `mariadb-mdb114-a` (same as
  drift-mariadb-client — orchestrator-supported).
