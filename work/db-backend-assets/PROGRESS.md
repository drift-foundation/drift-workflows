# DB backend as package assets — PROGRESS

See `README.md` (charter). Toolchain: **certified driftc 0.33.56 / ABI 18** (`~/opt/drift/certified/current`).

## Status: IMPLEMENTED + VERIFIED locally on 0.33.56 (awaiting commit → cert re-cut → post-cert review)

Implementation done; all repo-side verification GREEN on certified driftc 0.33.56:
- Relocations + scenario rework: `just test` GREEN (singular 16 ok, microflows component, **integration 165/165**), container self-torn-down.
- Manifest assets + version bump (singular 0.6.0 / microflows 0.2.0) + author-claim re-mint (top-level + components) → `drift author verify` in-sync, `drift trust check` ✓.
- Asset round-trip: `just deploy` packs `db/` INTO the `.zdmp` (no loose assets); `drift unpack` verify-gated + `--expect-version` → **production-only assets (no malformed/test/scenario)**; schema applies to `singular` AND `singular_5`.

### Verified consumer layout (CORRECTION)
`drift unpack --dest <t>` materializes the asset at its **declared path**: `<t>/singular/db` (NOT
`<t>/assets/singular/db`). Mariachi template path = `<t>/<pkg>/db`. db/README + plan corrected.

### Build-chain learnings (local gate)
- 0.33.56 re-cut the certified libs → committed component locks were stale (web-jwt/web-rest sha) → refreshed via `drift prepare` (microflows lib, runner).
- The runner BINARY build (parser-fixture addition) resolves microflows as a *package* from `build/dist/lib` → had to stage **microflows 0.2.0** locally (`drift deploy --dest build/dist/lib`) after the bump.
- **RESOLVED (cert-host bootstrap):** the runner `just build` previously resolved microflows as a
  *deployed package* from `build/dist/lib` — unsatisfiable on a fresh cert host (empty; microflows is the
  package under certification). Rewrote `microflows/runner/justfile build` to **compile microflows
  library source + runner app source together** (driftc, externals as `--dep` from the lock), mirroring
  the integration suite. Proven: removed the staged microflows package, runner builds from source
  (rc=0), parser fixtures 77/77. No pre-certified package needed.
- **Component locks refreshed for 0.33.56** (the re-cut certified libs changed external shas): microflows
  lib, runner, singular component, participant-stub — all `drift prepare`d. Final sweep: zero stale pins.

## Review findings (round) — all addressed

1. **Stub lock pinned pre-asset singular 0.5.0** → refreshed via `drift prepare` (now singular@0.6.0 +
   web shas). Zero stale 0.5/0.1 pins across all committed locks.
2. **Component manifests publish bumped pkgs without DB assets** (the component manifest can't declare
   `../db` — escapes its project root) → **guarded `singular/drift/justfile deploy`** to DEV-LOCAL
   staging only (`build/dist/lib`); refuses release dests so a same-version no-asset 0.6.0 can never be
   published. Canonical asset-bearing cut is the repo-root `just deploy` only. (microflows has no
   component deploy recipe.)
3. **Stale/placeholder docs** → removed the `example.invalid/mariachi` link in both `db/README.md`s;
   fixed the stale `microflows/db/scenarios/...` path in `integration/coordinator-singular/README.md`.

## Review findings (round 2) — one-signed-meaning invariant (HIGH)

Problem: same public version signed TWICE with different source identities — root asset-bearing claim
(singular 0.6.0 sci=c8d9…, with `singular/db`) vs component no-asset claim (sci=f5a1…); microflows
likewise (10c8… vs 3aae…). The no-asset component identity had already leaked into the stub lock. The
justfile destination guard was insufficient (direct `drift deploy` bypasses it; the alternate signed
identity still existed). **Invariant adopted: ONE signed meaning per public version** — singular 0.6.0 =
code + `singular/db`, microflows 0.2.0 = code + `microflows/db`, published ONLY from the root manifest.

Fix:
- **Deleted the component author-claims** (`singular/drift/drift/singular.author-claim`,
  `microflows/drift/microflows.author-claim`) + pubkey sidecars — no alternate signed identity exists.
- **Removed the component release recipes** (`singular/drift` `deploy`/`author-claim`/`build:deploy`;
  `microflows` `author-claim`); replaced with explicit NON-RELEASE notes. (Guard reverted — not the
  boundary.) Component manifests remain for local dev / source compilation + component tests only.
- **Apps source-dep our libs:** removed `singular` from the participant-stub and `microflows` from the
  runner/service `package_deps` (they compile our libs FROM SOURCE, like the integration). The runner now
  declares `mariadb-rpc` DIRECTLY (the source-compiled microflows lib's transitive external dep that was
  previously pulled via the microflows package_dep). Re-prepared → **no app lock resolves singular/
  microflows as a package** (verified: zero leaks across all committed locks).
- Cleaned the gitignored dev staging (`build/dist/lib`) of the no-asset packages.
- Re-verified the runner builds from source with NO microflows package present (parser fixtures 77/77).
- **Stub standalone build converted to source-compile** (round-2 follow-up): `microflows/participant-stub/justfile`
  `build` did a plain `drift build` resolving singular as a package — broken after the cleanup. Rewrote it
  to compile singular library source + stub app source (externals as `--dep` from the lock), like the
  runner/integration; dropped the `SINGULAR_LIB_ROOT` package-staging knob. Verified: stub builds from
  source with no singular package present (rc=0); lock has no singular. (`run`/`test-http` depend on it.)
  Now all three apps (runner, service, stub) compile our libs from source — no per-component package path.

## (historical) Status: READY TO IMPLEMENT on 0.33.56 (un-blocked — toolchain shipped the mechanism)

Sequencing (owner): **implement our side → post to cert (re-cert) → announce once it lands.** Defer the
pushcoin reply / consumer-contract publication until the certified asset-bearing build lands.

Literal next action: relocate `<pkg>/db/tests/` + `microflows/db/scenarios/` out of `db/` and update refs.

## Decisions locked

- **0.33.56 packed-asset mechanism**: declared `assets[]` are bundled INSIDE the validated `.zdmp`
  (content-addressed, covered by `artifact_sha256` + SCI; dirs expand recursively). Consumers materialize
  via verify-gated `drift unpack <pkg> --dest <dir> --trust-store <…>`; Mariachi reads the unpacked dir.
  No loose assets, no Drift "DB contract", **no toolchain work**.
- Ship whole production `<pkg>/db/` + `db/README.md`; manifest `"assets": ["<pkg>/db"]`.
- **MUST NOT pack malformed/test schema** — relocate fixtures out of `db/`; production-only gate on the
  UNPACKED tree.
- **`scenarios/` excluded entirely** — relocate `microflows/db/scenarios/` → `db-tests/` + rework the
  integration `mariachi scenario` seed (symlinked-base test template). `db/README.md` must NOT call
  scenarios included/optional.
- **Schema name = deployment scope** (`singular`/`singular_5`/`singular_canary`); production SQL is
  schema-agnostic (verified: 0 fixed qualifiers). Version pinning via `drift unpack --expect-version` /
  `--expect-sci`.
- **Bump versions** (additive public-API change): **singular 0.5.0 → 0.6.0**, **microflows 0.1.0 → 0.2.0**.
- **Each cert is a fresh, immutable cut** (nothing in-place) at the bumped versions.
- **Post-cert acceptance review** (owner): once the certified cut lands, K reviews the whole
  deployment/package end-to-end (`drift unpack` the landed cut, production-only, apply default+non-default,
  signatures/version/sha) BEFORE it's declared ready for pushcoin.

## Checklist (implementation)

- [ ] Relocate `singular/db/tests/`→`singular/db-tests/`; `microflows/db/tests/`→`microflows/db-tests/`; `microflows/db/scenarios/`→`microflows/db-tests/coordinator-fixtures/scenarios/` (+ symlinked base).
- [ ] Update refs: singular/justfile, singular/drift/justfile, microflows/justfile, emit_test_plan.py, sp_operation_test self-path, integration/coordinator-singular/justfile (scenario `--schema-template`) + test.py comment.
- [ ] Add `singular/db/README.md`, `microflows/db/README.md` (mariachi-compatible; deployment-scoped schema name + examples; `drift unpack`→mariachi example; scenarios/test fixtures NOT shipped).
- [ ] `drift/manifest.json`: add `"assets": ["singular/db"]` / `["microflows/db"]` + **bump version** (singular 0.6.0, microflows 0.2.0).
- [ ] **Dependency-resolution sweep**: run `drift prepare` (top-level + components); update stale pins to 0.6/0.2 — known: `microflows/runner/drift/manifest.json` (microflows 0.1 ×2), participant-stub singular constraint, locks. Nothing may resolve pre-asset 0.5.0/0.1.0.
- [ ] Re-mint author-claims at the bumped versions (SCI changes — assets + version); `drift author verify` in-sync + `drift trust check` OK.
- [ ] Verify: singular/microflows `just test` + integration 165/165 (reworked scenario seed) on 0.33.56.
- [ ] Verify: deploy on 0.33.56 → **`drift unpack` materializes DB assets from the deployed package**; assert `<dest>/assets/<pkg>/db` present + production-only (no tests/db-tests/malformed/_test/seed/scenarios; procs == production set); apply to `singular` AND `singular_5`.
- [ ] Coordinate re-cert = a fresh cut at 0.6.0/0.2.0 (cert-claim re-issue, new `artifact_sha256`) with the cert/orchestrator team.
- [ ] **Post-cert acceptance review**: `drift unpack` the LANDED certified cut end-to-end (signatures/SCI/sha/version, production-only, apply default+non-default) → declare ready for pushcoin.
- [ ] THEN: announce + reply to pushcoin with the consumer contract (asset-bearing versions 0.6.0 / 0.2.0).

## Open questions

- Re-cert version: **RESOLVED** — fresh cut, BUMP to singular 0.6.0 / microflows 0.2.0 (public-API change).
- Microflows scope: **DECIDED — bundle both now** (singular + microflows, same pass).
- (none open)

## Notes

- Out of scope: toolchain/Mariachi/orchestrator changes; consumer-repo (bookkeeper) wiring.
- Don't answer pushcoin until our side is implemented + re-cert lands (owner sequencing).
