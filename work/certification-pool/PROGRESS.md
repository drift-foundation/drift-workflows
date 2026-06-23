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

1. Reshaped `singular` → **0.5.0** — **IMPLEMENTED + GREEN** (was deferred; owner gave the spec and
   directed implementation). All four mutating transitions (`start`/`complete`/`fail`/`extend_lease`) are
   caller-clocked with absolute `std.time.UtcTimestamp` (`event_time` everywhere; `lease_expires_at` on
   start/extend; the public `lease_timeout_seconds` knob is removed). Strict monotonicity → thrown
   `EventTimeConflict` (errno 30002); `lease_expires_at > event_time` → thrown `InvalidLeaseExpiry`
   (30003); enforced client-side AND in the SPs (the SPs no longer read the DB clock for event time —
   DB time is audit-columns only). `WorkLease.lease_expires_at` is now a typed `UtcTimestamp`;
   `start`/`extend_lease` return the threaded `WorkLease`. **Behavior change:** extend is caller-
   authoritative (applies the proposed absolute deadline verbatim, validated `> event_time`); the pre-0.5
   "never shorten" flooring is GONE (it silently overrode the caller's time). Verified: singular 16/16
   (e2e + raw-SQL SP-invariants for both throws), stress, perf; integration **165/165** (stub on the 0.5
   API); top-level deploy stages **singular 0.5.0** + microflows 0.1.0 under the Foundation key. Ripple:
   gateway.drift, the 4 SPs (db/procs), the stub, all singular tests/scenarios, sp_invariants_test.py,
   both manifests, both locks (regenerated), top-level singular author-claim (re-minted 0.5.0).
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
- [x] **singular `just perf`** — fixed acquire→settle→inspect workload
      (`packages/singular/tests/perf/lease_cycle_perf.drift`, in-scenario `std.time` timing → JSON
      metric), gated by `tools/perf_gate.py` on `per_cycle_us` vs a **committed machine-keyed baseline**
      (`perf/baselines/<machine>.json`; 3× tolerance = cascading-slowdown guard) + exact cycle count.
      Wired via `emit_test_plan.py perf` + justfiles. **GREEN** (300 cycles, 633µs/cycle, baseline
      recorded). **Review fixes applied:** (1 High) a MISSING baseline now HARD-FAILS in gate mode —
      only `--update-baseline` records (was: auto-record+pass, a cert hole on a fresh host); (2 Med) per
      owner clarification, singular perf is **timing/throughput at the library/API level** with the real
      DB round trips in the workload — NOT SP-call/wire-byte counting (that is drift-mariadb-client's
      domain). Plan corrected to match.
- [x] **microflows `stress`** — concurrent-submit recovery race (`integration/coordinator-singular/
      stress.py`: 20 rounds × 8 runner processes racing one workflow id, asserts exactly-once participant
      dispatch (global exec-count +1/round) + a terminal winner), wired into the coordinator-singular
      `stress` recipe (compile apps + DB-lock + reset both schemas). **GREEN**.
- [x] **microflows `perf`** — service drive throughput (`integration/coordinator-singular/perf.py`:
      boot stub + microflows-service over a one-reserve-op manifest, time CYCLES=200 sequential submits,
      gate `per_wf_ms` vs a committed machine-keyed baseline; same hard-fail-on-missing + 3× tolerance as
      singular). Wired into the coordinator-singular `perf` recipe. **GREEN** (200 drives, 3.12ms/wf,
      baseline recorded).
- [x] **NEW top-level `drift/manifest.json` (+ author-claims, lock, trust)** — `drift/manifest.json`
      declares both artifacts (module paths rerooted into `singular/drift/...` + `microflows/...`),
      `drift/lock.json` prepared (both → mariadb-rpc@0.7.0 + mariadb-wire-proto@0.5.0). Bare
      `drift deploy --dest <tmp>` from the ROOT **stages BOTH** (`singular/0.5.0` + `microflows/0.1.0`).
      **GREEN.**
      **KEY DECISION (owner-confirmed):** the drift-workflows cert publication is signed with the
      **Foundation key** (`default.seed` / `ed25519:6DSIXZVQ…`) for BOTH artifacts — author + certifier —
      matching how drift-web / drift-mariadb-client are Foundation-published. `pushcoin.seed`
      (`ed25519:YvjbJdKV…`) is **reserved for `pushcoin/*` business apps** and is NOT used here. Top-level
      `drift/trust.json` unified to the one Foundation key across all namespaces. (NOTE: the *component*
      `singular/drift` author-claim/trust is still pushcoin-keyed — a legacy dev-only inconsistency,
      irrelevant to the cert build, which uses the top-level manifest/trust; flag for re-keying.)
- [x] **Root aggregation reconciled.** Root `just test/stress/perf` already aggregate singular +
      microflows + `_integration-gate`. The microflows COMPONENT `stress`/`perf` stubs were rewritten from
      "no scenarios" to honest pointers: the coordinator stress/perf are inherently cross-component and
      live in `integration/coordinator-singular` (run via `_integration-gate`). No misleading "no stress".
- [x] **`stage_packages` shape confirmed.** Root has no deploy recipe (correct) — the orchestrator runs a
      bare `drift deploy --dest {libs_root}` from the checkout root. Verified it emits cert claims +
      requires the orchestrator-supplied cert-suite evidence (a no-evidence/no-flag deploy correctly
      refuses); with evidence supplied, stages both. No cert-suite flags in our recipes.
- [x] **DB / Mariachi env contract** captured for the orch team (MariaDB 127.0.0.1:34114, `MDB_ROOT_PWD`,
      flocker `mariadb-mdb114-a`, **Mariachi ≥ 1.0.0** — the one external tool to provision; `MARIACHI_BIN`
      override). See ORCH_MESSAGE.md §4.
- [x] **Orch-team message drafted** → `work/certification-pool/ORCH_MESSAGE.md` (single entry +
      dep/affects, single-entry/package-root rationale, Foundation-key signing, the three gates + their
      meaning, env+Mariachi contract, committed machine-keyed perf baselines + the cert-host baseline
      ask, version pins + 0.5.0 sequencing, monorepo/split alternatives).
- [ ] **Final validation:** full root dry-run (`just test` → `just stress` → `just perf`) in one sweep
      under the staged toolchain/libs. Each gate is individually GREEN; this confirms the aggregation
      order end-to-end. (~15 min; not yet run as a single sweep.)
- [x] **Foundation re-key — repo-wide (owner: "Foundation owns this repo").** Swept `pushcoin`/
      `ed25519:YvjbJdKV…` out of singular entirely → the Foundation key (`ed25519:6DSIXZVQ…` /
      `default.seed`): component `singular/drift` trust.json, author-claim (re-minted), manifest
      `author_profile` → `the-drift-foundation.author-profile` (pushcoin profile deleted), and the
      participant-stub trust.json + lock (regenerated: singular@0.5.0, Foundation author) + manifest
      constraint (`singular 0.5`). `drift trust check` passes (✓ singular ✓ microflows, trust-v1 ready).
- [x] **Convention alignment (owner: "don't invent — look at drift-web / drift-mariadb-client").**
      Removed singular's bespoke evidence ceremony (the `cert` recipe + invented `drift-foundation/
      singular` cert-suite + `*.cert-evidence/1` schema). Added repo-ROOT `author-claim` / `prepare` /
      `trust-check` / `reseal` / `deploy` recipes mirroring drift-mariadb-client (multi-artifact, mints
      singular + microflows from the top-level manifest via `DRIFT_SIGN_KEY_FILE`/`default.seed`; `deploy`
      = `--cert-suite-id drift-workflows/dev --cert-suite-no-evidence`, orchestrator overrides + binds
      real evidence). Verified: root `reseal` green + root `deploy` stages singular/0.5.0 + microflows/
      0.1.0. Component `singular/drift` deploy simplified to the same convention.

## Message review fixes (round 2)

- **(High) singular 0.4.1 not offered for official cert.** ORCH_MESSAGE.md was reframed accordingly.
  **SUPERSEDED:** the 0.5.0 reshape is now implemented + green (decision #1), so the ask is **both**
  `microflows 0.1.0` + `singular 0.5.0` together — the manifest stages `singular 0.5.0` (0.4.x is never
  submitted). No sequencing question remains.
- **(Med) Foundation author-profile now lists `singular.*`** (was microflows-only) — consistent with the
  top-level manifest publishing both + the singular author-claim using the Foundation key.
- **(Low) perf_gate.py docstring corrected** — no longer says "first run records + passes"; states the
  missing-baseline HARD-FAIL behavior.

## Next action

Cert-pool alignment is **functionally complete**: 4 gates green, top-level manifest stages both under the
Foundation key, orch message drafted. Remaining = the optional full root dry-run sweep (each piece already
green individually) + the non-blocking component-singular re-key follow-up.

## Notes

- Do NOT edit build-orchestrator; craft a message for the orch team instead.
- Do NOT treat local `build/dist/lib` as cert output; do NOT hand-edit locks/claims.
- DB tests need MariaDB 127.0.0.1:34114 + MDB_ROOT_PWD + flocker `mariadb-mdb114-a` (same as
  drift-mariadb-client — orchestrator-supported).
