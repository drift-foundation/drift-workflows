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
  Gate re-verified green at 165/165 on **0.33.54/abi18** (test + stress + perf). 0.33.53 has a driftc
  codegen OOM now sidestepped by the data-driven parser gate — see the round at the end + ORCH_MESSAGE §0.

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
- [x] **Final validation — full root sweep GREEN.** `just test → just stress → just perf` run as one
      sweep under the staged toolchain/libs (all exit 0): **test** = singular 16/16 + microflows component
      (sp_operation 110/110 via the MARIACHI_BIN-derived python + runner IR 3/3) + integration 165/165;
      **stress** = singular + microflows (exactly-once dispatch held); **perf** = singular 646µs vs 633
      baseline + microflows 3.04ms vs 3.12 baseline (both PASS). Confirms the re-key, re-mint, the Mariachi
      fix, and scenario 17 end-to-end through the orchestrator's root entry.
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

Cert-pool alignment is **complete**: singular 0.5.0 reshape green; Foundation re-key (trust-check passes);
drift-web/mariadb-client convention (root author/deploy recipes); top-level manifest stages both under the
Foundation key; **full root sweep test→stress→perf GREEN end-to-end**; orch message corrected.

**Orch-team review round (all addressed):** (1) stale microflows claim — re-minted, SCI now matches the
orchestrator's recompute (`0a584…`), trust-check ✓; (2) upstream retest — **SUPERSEDED by the final round
below: `affects` was removed from the orchestrator config model entirely; invalidation is reversed
`depends_on`, so NO `affects` edges / upstream edits are needed (the earlier "request `*.affects`" ask is
obsolete).** net-tls correctly stayed out of our `depends_on` (zero `net_tls` imports; transitive via web);
(3) MARIACHI_BIN bypass — `_test-sp` now derives the venv python from the resolved env;
(4) readiness vs evidence — the full root sweep is now run + green, so "ready" is evidence-backed.

**Cert attempt #1 (`[singular] load schema → mariachi venv missing`):** part env, part repo. The
schema-load itself correctly honors `MARIACHI_BIN` (singular/justfile:18) — the orch just hadn't set it
(the relative dev default can't resolve under a fresh checkout). BUT a repo-side sweep found **more
hardcoded mariachi-python paths** the round-3 fix missed: `singular/drift/tools/emit_test_plan.py`
(the cert plan's SP-invariants job) + `singular/drift/justfile` `test-sql`. Both now derive the venv
python from `MARIACHI_BIN` (relative = dev fallback); a comment in `microflows/db/tests/sp_operation_test.py`
updated too. Verified: `emit_test_plan` emits `<MARIACHI_BIN-dir>/python` when set, the relative fallback
when unset; **`singular test` 16/16 with `MARIACHI_BIN` exported to an absolute path**. ORCH_MESSAGE §4
now states `MARIACHI_BIN` is REQUIRED under cert + that nothing in the repo hardcodes a Mariachi path.

## Capabilities-contract adoption (`DRIFT_CERT_CAPABILITIES`) — supersedes the per-tool env approach

> **HISTORICAL — capability surface SUPERSEDED.** This round adopted `requires:["tool:mariachi",
> "service:mariadb"]`. Both later rounds changed that: `service:mariadb` was **removed** (MariaDB is a
> repo-private Docker fixture), then **`tool:docker` was added**. The CURRENT contract is
> `requires:["tool:mariachi","tool:docker"]` — see the two "## Round:" entries at the end of this file.

The orchestrator moved to a formal external-capabilities contract (build-orchestrator
docs/certification-onboarding.md §5-7 + work/cert-capabilities/WORKFLOWS_ADOPTION.md): it injects ONLY
`DRIFT_CERT_CAPABILITIES` → a `capabilities.json`; it no longer sets `MARIACHI_BIN`/`DB_*`. We declared
`requires:["tool:mariachi","service:mariadb"]` (orch confirmed it's wired in `orchestration.json`) and
adopted it:

- **`tools/cert-env.sh`** — one root shim, sourced at the top of every DB-backed recipe (singular +
  microflows + integration justfiles). Two-mode: cert (doc authoritative, **fail-early** on a missing
  `tool:mariachi.bin` / `service:mariadb.{host,port,credential_env}`; password normalized into
  `MDB_ROOT_PWD` via env-name indirection, secret never serialized) / local (repo defaults +
  `MARIACHI_BIN`/`MDB_ROOT_PWD` overrides). Parsed with **python3, no `jq`** (per directive). The doc
  carries NO `lock_key`/`user` (confirmed against the updated onboarding §6); the corrected shim does not
  require them.
- **`mariadb` CLI dependency removed** — **SUPERSEDED by the final round below: `tools/load_sql.py` was an
  interim step and is now DELETED; ALL DB population (incl. both test fixtures) goes through Mariachi
  (separate `singular_malformed` / `microflows_test` schemas).** Cert surface is exactly `tool:mariachi` +
  `service:mariadb` (+ toolchain-provided flocker).
- **DB lock is repo-owned + project-scoped** (`drift-workflows-mdb114-a`, not the shared-instance key) per
  adoption-doc §3 — avoids over-serializing other repos on the shared MariaDB box.
- Harnesses (`test.py`/`stress.py`/`perf.py`/`sp_operation_test.py`/`sp_invariants_test.py`) + the two
  Drift stress/perf scenarios read `DB_HOST`/`DB_PORT` from the resolved env.
- **Verified BOTH modes, all three gates** (onboarding §4 hand-written `capabilities.json`):
  - **cert mode** (`DRIFT_CERT_CAPABILITIES` set): `test` **165/165**, `stress` exactly-once held,
    `perf` singular 676µs/633 + microflows 2.942ms/3.12 (both PASS) — Mariachi + DB sourced from the doc,
    no `MARIACHI_BIN`/`DB_*` from ambient env;
  - **local mode** (unset): `test` green (component 20/20 + 110/110 + 3/3, integration 165/165), stress +
    perf green.

## Notes

- Do NOT edit build-orchestrator; craft a message for the orch team instead.
- Do NOT treat local `build/dist/lib` as cert output; do NOT hand-edit locks/claims.
- DB tests need MariaDB 127.0.0.1:34114 + MDB_ROOT_PWD + flocker `mariadb-mdb114-a` (same as
  drift-mariadb-client — orchestrator-supported).

## Round: cert-compliance reviews (two teams) + Mariachi-for-everything + affects removal

- **Findings 1+2 (hardcoded DB endpoint in cert-gate tests):** the 6 test files (3 microflows e2e,
  singular live_gateway / malformed_backend_test, sp_invariants_test.py) now read host/port/user/password
  from env (defaults preserved for local). Fixed a `host` binding vs `import microflows.host` collision
  (`host`→`db_host`).
- **mariadb CLI removed from the gate path → Mariachi (owner directive: use Mariachi, not load_sql.py).**
  Both raw-SQL fixtures migrated to **separate Mariachi-managed test schemas**: `singular_malformed`
  (procs-only, `singular/db/tests/malformed`) and `microflows_test` (`microflows/db/tests/seed`, whose seed
  proc writes into `microflows.*` with qualified names). Confirmed Mariachi loads procs-only/malformed
  templates (the DELIMITER wrapper is required, like the real procs). `tools/load_sql.py` + both raw `.sql`
  DELETED; `test-malformed-fixture` dev recipe re-routed through Mariachi. Note: mariadb-rpc `conn.call`
  rejects a dotted proc name, so the seed connection targets `microflows_test` and calls the proc
  unqualified. Only remaining mariadb-CLI use is the dev-only `db-sql` (NOT on any gate path; marked so).
- **Orch review — `affects` removed from the config model:** deleted `affects:["bookkeeper"]` from the §1
  entry (a lingering `affects` is now a hard load error); reversed the stale "invalidation from affects"
  text — the orchestrator reverses `depends_on`, so our `depends_on:[drift-lang,drift-mariadb-client,
  drift-web]` already auto-retests us on an upstream bump (net-tls→web→workflows transitively); NO upstream
  edits needed. Entry is now exactly path/kind/depends_on/requires/commands.
- **§3 lock key reconciled** to the project-scoped `drift-workflows-mdb114-a` (matches §4 + cert-env.sh);
  dropped the "same shared group as drift-mariadb-client" framing.
- **Shim:** secret check tightened to reject empty-but-set (`in (None, "")`).
- **Verified BOTH modes, all three gates:** local + cert (hand-written capabilities.json) — test 165/165 +
  110/110 + 3/3, stress exactly-once, perf within baseline. All exit 0.

## Round: tool:docker capability + gate teardown (orch follow-up)

Orch declined "Docker kept to ourselves" — sandboxing may block spawning a container, so it must go
through the capability model (onboarding doc updated with a Docker-capability section + "gates restore
entry state"). Addressed:
- **Declared `tool:docker`** → `requires: ["tool:mariachi", "tool:docker"]`. `tools/cert-env.sh` resolves
  BOTH tool bins in cert mode (`MARIACHI_BIN` + `DOCKER_BIN`), fails early if either capability/bin is
  missing; local mode defaults `DOCKER_BIN=docker`. `db_instance.sh` uses the resolved client + checks
  daemon liveness (preflight verifies the client; daemon is ours).
- **Image pinned by digest** — `mariadb@sha256:2f45480c…` (determinism; no run-time tag pull).
- **Teardown = restore entry state (REQUIRED).** Root `test`/`stress`/`perf` capture whether they STARTED
  the container (`db_instance.sh up` now reports STARTED/RUNNING) and trap-`down` on exit (success OR
  failure) ONLY if they started it; a pre-existing container (dev box) is left. Nesting-safe (sub-gates
  see RUNNING). Inner schema-setup `up` is idempotent.
- **`db_instance.sh sql` subcommand** added; the stale `clean-data` dev recipe (mdb114-a / two-arg call)
  rewired to it via the shim.
- **VERIFIED both modes + teardown:** local & cert test/stress/perf all exit 0 with the container ABSENT
  after each gate that started it (165/165 integration, 110/110 SP, perf within baseline); and a
  pre-existing container is correctly LEFT running. Capability surface is honestly `tool:mariachi` +
  `tool:docker` — MariaDB is a repo-private, self-torn-down Docker fixture.

## Round: parser unit test made data-driven (drop the 5 GB compile) + re-validate on 0.33.54

The microflows parser unit test was a single ~520-line `scenario()` in
`runner/tests/unit/parser_test.drift` that inlined ~60 `.mf` scenarios + hand-authored expected graphs
into ONE driftc translation unit — **~5.4 GB / ~4 min PER VARIANT to compile** (and it tripped the
0.33.53 codegen OOM, fixed in 0.33.54). Testing a parser must not cost 5 GB. Reworked to the standard
compiler-test pattern (owner: "produce the mf-compiler; the binary reads these tests"):
- **New `microflows-runner --parse-check FILE` mode** (`runner/src/runner.drift`): parse a `.mf` source,
  emit a canonical JSON outcome (`status`/`is_graph`/`canonical`/`validate`/`type_check`, or
  `code`+byte/line/column for a parse error) to stdout; DB-free, registry-free. Reuses the existing
  `--lower-source` for the 3 lower-overlay/strip/unknown-op cases.
- **Fixture corpus** `runner/tests/fixtures/parser/{check,lower}/*.mf` (74 + 3) + committed goldens,
  blessed from the currently-passing parser and **cross-checked against the old test's exact assertions**
  (diagnostic codes + byte/line/column incl. the multibyte `café` case, is_graph, validate/type rejects,
  rename→identical-canonical parity, lower overlay/strip/unknown-operation).
- **Harness** `runner/tests/run_parser_fixtures.py` (run + `--update`). The runner `just test` now builds
  the binary once + golden-diffs the corpus: **77/77 in 0.082 s** (KB RAM). The heavy compile is preserved
  OFF the gate path as `just test-compiler-stress` (a deliberate driftc-codegen regression canary — it
  caught the 0.33.53 OOM). Fixed `runner` `just build` to select the `microflows-runner` artifact.
- The certified `microflows` LIBRARY artifact is untouched (the change is in the runner app, compiled from
  source by the integration gate). No re-mint / lock change; abi18 unchanged.
- **Re-validated all three gates on 0.33.54/abi18:** `test` (integration 165/165 + runner fixtures 77/77),
  `stress` (20×8 exactly-once held), `perf` (microflows 3.312 ms vs 3.12 baseline → PASS). DB container
  self-torn-down. See `work/parser-fixture-tests/` for the PLAN/PROGRESS of this sub-effort.
