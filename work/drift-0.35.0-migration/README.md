# Drift 0.35.0 / ABI 22 alignment — lambda-contract release train

## Short-term objective

Align drift-workflows with staged `drift-0.35.0+abi22`
(`DRIFT_TOOLCHAIN_ROOT=/home/sl/opt/drift/staged/toolchain/drift-0.35.0+abi22`,
`DRIFT_PKG_ROOT=/home/sl/opt/drift/staged/libs`), run all cert gates
(test, perf, stress), release as singular **0.10.0** / microflows **0.9.0** /
uflowsd **0.8.0** (MINOR bumps — accepted review finding: strict JSON is an
acceptance-contract tightening), reseal (author-claim + lock + trust-check)
so the repo is ready for cert on the staged toolchain.

## Current behavior / problem

0.35.0 (per /tmp/drift-announce/2026-08-05T23-42-56Z-drift-lang-release-notes.md)
reworks lambda return typing, divergence classification, and lambda lowering:

- Inferred lambdas now REJECT incompatible returns across control-flow paths.
- All-throw / non-fallthrough lambda bodies lower as unreachable (previously
  bogus return values or ICEs).
- v1 stored-lambda contract: a bare capturing lambda value cannot be stored;
  escaping captures must use `core.callbackN(...)` (captureless stored lambdas
  keep the fn-pointer route).
- Unqualified `Ok(...)` resolves as ordinary `core.Result` variant constructor
  (legacy `HResultOk` rewrite removed).
- Diagnostics around poisoned lambda bindings/captures re-worded; exact-message
  tests may need intentional updates.

Pre-scan of our sources: zero unqualified `Ok(` sites; stored callbacks already
go through `core.callback1` (singular/gateway.drift). ~2.5k lambda-ish lines —
the compiler run is the authoritative audit, not textual sweeps.

## Accepted design decisions

- Migrate strictly from compiler diagnostics; recompile to convergence.
- No compatibility aliases or source workarounds for replaced behavior (release
  note directive); any suspected parser/checker/lowering/codegen/runtime defect
  = CORE_BUG/LANGUAGE_BUG: stop, minimal repro, report; no source-level evasion.
- Deps repinned to the staged 0.35.0 pool (all one patch ahead of certified):
  mariadb-rpc 0.8.1→0.8.2, mariadb-wire-proto 0.6.1→0.6.2, net-tls 0.6.3→0.6.4,
  web-client 0.5.4→0.5.5, web-jwt 0.5.3→0.5.4, web-rest 0.6.4→0.6.5 — all five
  lock.json files (root, microflows, singular, runner, participant-stub).
- MINOR version bumps (0.10.0/0.9.0/0.8.0) — SUPERSEDES round 1's patch-bump
  decision, which review rejected: strict parse at the ~10 pre-existing
  production `json.parse` boundaries tightens the acceptance contract
  (MariaDB JSON_VALID admits duplicate keys, so the backend never guaranteed
  uniqueness). Root drift/manifest.json is the sole version authority;
  RUNNER_VERSION kept in sync (emit preflight enforces); uflowsd's microflows
  range widened 0.8→0.9.
- Compiler floor 0.35.0 ENFORCED fail-closed (tools/cert_deps.py): at --dep
  derivation for dep-resolving compiles, at plan-emit time in all four
  emitters for dep-free ones, and via `--check-floor` in the runner's
  direct-driftc loop. Nonzero driftc exit rejected before stdout parse;
  pinned by tools/tests/test_cert_deps_floor.py (gated job
  cert-deps-floor-test).
- Duplicate-key rejection pinned top-level + nested at three Drift
  boundaries; DB-side parity BLOCKED on the MariaDB 12.3 migration
  (work/mariadb-12.3-json-parity/).
- Gates on staged toolchain with DRIFT_TEST_JOBS=8; compare perf against
  0.33.91-round baselines (singular ~643us, microflows ~3.11 ms/wf).

## Verification criteria

- `just test` green (drift combined plan **66 ok / 0 failed** — 61 original
  + 4 strict_json_test jobs + cert-deps-floor-test — plus microflows-viz
  62/62).
- `just perf` within noise of baselines; `just stress` green.
- `just reseal` green (author-claims re-minted at 0.10.0/0.9.0/0.8.0, lock
  resolved, trust-check OK).
- Announce note published to /tmp/drift-announce/ per house style,
  superseding earlier round notes.

## Change log

- Round 1 (2026-08-05): locks repinned via `drift prepare` in all five
  projects against staged pool; full `just test` launched as migration probe.
  parse_strict→parse rename (29 sites); patch bumps 0.9.3/0.8.3/0.7.4; gates
  green; resealed; announce published (2026-08-06T05-40-31Z note).
- Round 2 (2026-08-06, review feedback): the patch-bump round was REJECTED on
  two findings, both accepted:
  1. Floor: repo advertised >= 0.33.91; on such a compiler the renamed
     `json.parse()` sites compile but parse PERMISSIVELY, silently defeating
     all 29 strict migrations. Fixed: floor raised to 0.35.0 and ENFORCED in
     tools/cert_deps.py (`driftc --version --json`, fail-closed; verified
     pass-at-floor + reject-at-0.34.1), every ">= 0.33.91" message/doc text
     updated.
  2. Versioning: strict parse at ~10 pre-existing production `json.parse`
     boundaries (caller JSON, HTTP request/response payloads, operator
     manifest) is an acceptance-contract TIGHTENING — MariaDB's JSON_VALID
     accepts duplicate-key objects, so the backend never guaranteed
     uniqueness. Minor bumps: singular 0.10.0, microflows 0.9.0, uflowsd
     0.8.0 (+ RUNNER_VERSION, + uflowsd microflows range 0.8→0.9); changelog
     entries added (singular/history.md, microflows/CHANGELOG.md).
  Duplicate-key rejection pinned at three boundaries (top-level + nested per
  user directive): runner unit test tests/unit/strict_json_test.drift (wired
  into root emitter + runner justfile), singular malformed-backend fixture
  keys 0x0F/0x10, and two manifest_dupkey_* runner fixtures (goldens
  verified, 13/13). User decisions recorded: keep strict Drift validation;
  DB-side parity blocked on the MariaDB 12.3 migration (IS JSON OBJECT WITH
  UNIQUE KEYS guards + DB-boundary pins) — charter in
  work/mariadb-12.3-json-parity/.
