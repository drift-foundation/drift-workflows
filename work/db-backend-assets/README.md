# DB backend as package assets

## Short-term objective

Ship each certified package's production MariaDB backend (`<pkg>/db/`) as a declared package asset
bundled **inside the validated `.zdmp`** (driftc 0.33.56), so a cert-only consumer (bookkeeper)
materializes it from the certified path with `drift unpack` and applies it via Mariachi. No Drift "DB
contract" concept; no toolchain work (0.33.56 delivers the mechanism).

## Status: READY TO IMPLEMENT on driftc 0.33.56 / ABI 18

Previously blocked on toolchain support for assets-inside-`.zdmp` + a verify-gated unpack — **DELIVERED
in certified driftc 0.33.56** (`drift deploy`/`build` pack `assets[]` into the `.zdmp`; `drift unpack`
verifies-then-materializes, fail-closed/atomic/TOCTOU-safe). pushcoin verified `drift unpack` works
against their trust store. This is now an implementation + re-cert task, not a toolchain-blocked design.

## Current behavior / problem

The certified `singular` / `microflows` packages ship only compiled Drift; their runtime MariaDB
backend (`<pkg>/db/`, applied by Mariachi) is not on the certified path. The currently-certified
`singular/0.5.0` (a 2026-06-23 build) ships **no asset** (`drift unpack … → assets:[]`), so there's
nothing to consume yet; bookkeeper works only against a *residual* schema on an old shared DB — not
reproducible on a clean/cert host. pushcoin asks us to **re-cert singular (and microflows) with the
mariadb schema bundled as a declared asset** (refs: pushcoin
`2026-06-25T13:10:24Z`, drift-lang `2026-06-25T*` 0.33.56 notes).

## Accepted design decisions

- **Package `assets` mechanism (0.33.56)** — declared `artifacts[].assets[]` entries are packed into
  the `.zdmp` as content-addressed blobs; **directory entries expand recursively**. Integrity binding:
  the assets are inside the `.zdmp`, so the **verified artifact hash (`artifact_sha256`) covers the
  packed asset bytes**, while **SCI / author-claims bind the authored source+asset identity**. Drift
  stays DB-unaware; no toolchain change needed.
- Ship the whole production `<pkg>/db/` tree + a `db/README.md`; manifest `"assets": ["<pkg>/db"]`.
- **HARD REQUIREMENT (MUST): never pack the malformed/test schema.** Because a declared dir is packed
  recursively, fixtures under `db/` would ship — so they are relocated OUT of `db/` AND a production-only
  gate (run on the UNPACKED tree) hard-fails if any reach the asset (see Verification).
- **Schema/database NAME is DEPLOYMENT scope, not certified identity** — the consumer picks the live
  schema name (`singular`, `singular_5`, `singular_6`, `singular_canary`, …) for versioned coexistence /
  canarying. Production SQL is schema-agnostic (verified: singular 0 fixed qualifiers; microflows only a
  `microflows.state` *code comment*); Mariachi applies into `--schema <name>`.
- **`scenarios/` EXCLUDED entirely** (production-only published asset): relocate
  `microflows/db/scenarios/` out to `db-tests/` (never packed) and rework the integration `mariachi
  scenario` seed (symlinked-base test template — scenario resets base + overlays from one template).
  `db/README.md` must NOT describe scenarios as included or optional.
- **Bump versions (additive public-API change):** shipping the DB backend as a consumable, versioned
  asset extends the package's public surface, so cut fresh versions — **singular `0.5.0 → 0.6.0`**,
  **microflows `0.1.0 → 0.2.0`** (minor; additive + backward-compatible — code-only consumers ignore
  assets). The asset-bearing cut is therefore `0.6.0` / `0.2.0`, NOT the pre-asset `0.5.0` / `0.1.0`.
- Runtime schema↔version stamp: deferred — consumers pin via `drift unpack --expect-version` /
  `--expect-sci` (below).

## Asset Integrity

drift 0.33.56 stores declared package assets inside the validated `.zdmp`. Consumers materialize assets
with `drift unpack <pkg-dir> --dest <dir>`, which verifies the package (author+cert signatures, SCI
equality, artifact hash, provenance) BEFORE writing files — fail-closed, atomic, TOCTOU-safe; a trust
source is required (no silent self-trust). Mariachi reads only the unpacked filesystem directory; it
does not verify package assets itself. No loose, unverified assets on the trusted path.

Trusted consumer path:
```bash
drift unpack "$DRIFT_PKG_ROOT/singular/0.6.0" --dest "$tmp" --trust-store <trust> \
  --expect-version 0.6.0 --expect-sci sha256:<resolved>
mariachi --schema-template "$tmp/singular/db" … apply --schema singular_5
```

## Consumer contract (answers to pushcoin)

- **Materialized layout under `--dest`:** `<dest>/<declared-path>`. Declaring `assets:
  ["singular/db"]` yields a Mariachi `--schema-template`-shaped dir at
  `<dest>/singular/db/{schema,procs,constants,grants,README.md}` — point Mariachi straight at it.
- **Schema name:** default/conventional `singular` (matches the gateway's DB-config block), but the name
  is **deployment scope** — apply to `singular`, `singular_5`, `singular_6`, … as needed; it must match
  the schema the consumer's gateway connects to. (Same for `microflows`.)
- **Version pinning:** `drift unpack` supports `--expect-version 0.6.0` and `--expect-sci sha256:<…>`,
  asserted BEFORE extraction — pass the version/SCI resolved from the consumer's lock/`prepare` to bind
  the unpacked schema to the resolved package rev. (Also pin by package path from lock/prepare.) Note the
  asset-bearing version is `0.6.0` (singular) / `0.2.0` (microflows), a bump from the `0.5.0`/`0.1.0`
  pushcoin referenced — their lock re-`prepare`s against the new version.

## Target shape (after `drift unpack`)

```
<unpack-dest>/singular/db/{README.md,schema,procs,constants,grants}
<unpack-dest>/microflows/db/{README.md,schema,procs,constants,grants}
```
Manifest declares the production template dir only: `"assets": ["singular/db"]` / `["microflows/db"]`.

## Concrete implementation plan

1. **Relocate ALL test material out of `db/`** (dir-expansion packs everything under it → keep it
   production-only):
   - `singular/db/tests/` → `singular/db-tests/` (self-contained `malformed/` template).
   - `microflows/db/tests/` → `microflows/db-tests/` (`seed/` template + `sp_operation_test.py`).
   - `microflows/db/scenarios/` → `microflows/db-tests/coordinator-fixtures/scenarios/` + a symlinked
     base (`schema|procs|constants → ../../db/<dir>`) so the scenario template is mariachi-applicable.
   - Update references (recipes/comments; `.drift` tests key off schema names): `singular/justfile`,
     `singular/drift/justfile`, `singular/drift/tools/emit_test_plan.py`, `microflows/justfile`,
     `integration/coordinator-singular/justfile:77,117,158` (scenario `--schema-template`) + test.py
     comment.
2. **Add `<pkg>/db/README.md`** (singular + microflows): DB backend material for *this package version*;
   Mariachi-compatible; **schema name is deployment-scoped** with examples (`--schema singular`,
   `--schema singular_5`, `--schema singular_canary`); example
   `drift unpack … --dest <t>` then `mariachi --schema-template <t>/<pkg>/db apply --schema <name>`;
   consumers MAY deploy the SQL via another reviewed process; **test fixtures and scenarios are excluded
   (not shipped)** — wording must NOT say scenarios are included/optional.
3. **Manifest + version bump + dependency-resolution sweep.** In `drift/manifest.json` add
   `"assets": ["singular/db"]` / `["microflows/db"]` AND bump `version` (singular `0.5.0 → 0.6.0`,
   microflows `0.1.0 → 0.2.0`). Then **run `drift prepare` (top-level + components) and sweep locks +
   manifests + integration configs for anything still resolving `singular 0.5` / `microflows 0.1`** —
   known: `microflows/runner/drift/manifest.json` pins `microflows 0.1` (×2); also the participant-stub's
   singular constraint and the top-level/component `drift/lock.json`s — update to `0.6` / `0.2`. The
   asset-bearing version is the actual consumer target, so nothing may still resolve the pre-asset
   `0.5.0`/`0.1.0`.
4. **Author-claim refresh:** we currently declare no assets, so declaring them changes the SCI → re-mint
   both author-claims (Foundation key `default.seed`, repo-root `drift author` recipe); then `drift
   author verify --artifact {singular,microflows}` → in sync and `drift trust check` → OK.
5. **Re-cert on 0.33.56 = a FRESH CUT (nothing in-place).** Each certification is a fresh, immutable cut
   — not a mutation of the existing `0.5.0`/`0.1.0`. The fresh cut carries the **BUMPED versions**
   (`singular 0.6.0` + `microflows 0.2.0`), re-minted author-claims, and re-issued cert-claims (new SCI +
   `artifact_sha256` from the added asset). ABI 18 unchanged; consumers `prepare` their lock against the
   new version + sha (no rebuild).
6. **Post-cert acceptance review (before pushcoin handoff).** Once the certified cut lands, review the
   WHOLE deployment/package end-to-end before declaring it ready for pushcoin: `drift unpack` the LANDED
   certified cut against our trust store (signatures / SCI / `artifact_sha256` / provenance pass);
   confirm `<dest>/<pkg>/db` is the production-only template (no test/scenario material; procs ==
   production set); confirm the version/sha; apply the unpacked schema to default + non-default schema
   names; confirm the consumer contract (layout / schema name / `--expect-version`+`--expect-sci`) holds
   as documented. Only then announce + reply to pushcoin.

## Files likely affected

- `drift/manifest.json`; `drift/{singular,microflows}.author-claim` (re-minted)
- New: `singular/db/README.md`, `microflows/db/README.md`
- Moves: `singular/db/tests/` → `singular/db-tests/`; `microflows/db/tests/` → `microflows/db-tests/`;
  `microflows/db/scenarios/` → `microflows/db-tests/coordinator-fixtures/scenarios/` (+ symlinked base)
- Recipe/harness refs: `singular/justfile`, `singular/drift/justfile`, `microflows/justfile`,
  `singular/drift/tools/emit_test_plan.py`, `integration/coordinator-singular/justfile`, `…/test.py`
- Coordinate the re-cert (cert-claim re-issue) with the cert/orchestrator team.

## Verification criteria

1. **Gates green after moving fixtures** (driftc 0.33.56): `cd singular && just test` (loads
   `singular_malformed` from `db-tests/malformed`); `cd microflows && just test` (loads `microflows_test`
   seed + `sp_operation_test.py` from `db-tests/`); root `just test` integration 165/165 (proves the
   reworked symlinked-base `mariachi scenario` seed still resets+seeds `coordinator-fixtures`).
2. **Deploy with 0.33.56** (assets packed into the `.zdmp`).
3. **`drift unpack` the deployed `singular` AND `microflows`** (`--trust-store <ours>`) → verify passes.
4. **Assert `<dest>/<pkg>/db` exists and contains NO test/scenario material** — no
   `tests/`/`db-tests/`/`malformed`/`*_test`/`seed`/`scenarios`; published `procs/` equals exactly the
   production proc list.
5. **Apply the unpacked schema to BOTH default (`singular`) AND non-default (`singular_5`)** against a
   clean DB — both succeed (deployment-scoped naming + coexistence). Same for microflows.
6. **Signing intact:** `drift author verify` in sync; `drift trust check` OK.

## Current status and next action

Status: **READY TO IMPLEMENT on driftc 0.33.56.**

Sequencing (owner): **implement our side first → post to cert (re-cert) → announce once it lands.**
Do NOT reply to pushcoin or publish the consumer contract yet — defer all external answers until our
side is implemented and the re-cert lands. The "Consumer contract" section above is pre-drafted as the
*announcement payload* for that point, not an action to take now.

Next action: implement steps 1–4 (fixture/scenario relocation + ref updates → `db/README.md` ×2 →
manifest `assets` → author-claim refresh), verify (incl. `drift unpack` round-trip), then coordinate the
re-cert (step 5). Announce + reply to pushcoin only after the certified asset-bearing build lands.

## Open questions / blockers

- ~~Re-cert in place vs bump~~ — RESOLVED: nothing is in-place (each cert is a fresh cut), and we BUMP
  (singular `0.6.0`, microflows `0.2.0`) as an additive public-API change.
- **Microflows scope: DECIDED — bundle both singular + microflows now** (same pass, one re-cert covers
  both). pushcoin only required singular, but the change is identical, so microflows ships too.

## Relevant review findings

- Effort un-blocked: 0.33.56 certified, mechanism delivered → implementation/re-cert task (not toolchain ask).
- Toolchain moved staged-0.33.54 → certified-0.33.56/ABI 18 (ABI unchanged; bytes/cert-claims change on bundling).
- Asset-integrity section rewritten to the now-supported trusted path (`drift unpack` → Mariachi).
- Verification asserts UNPACKED assets (`drift unpack` the deployed package), not loose package-dir assets.
- README answers pushcoin's questions (layout, deployment-scoped schema name, `--expect-version`/`--expect-sci`).
- "No toolchain work" true again; prior blocker/ask is historical context.
- Scenarios stay excluded/relocated; `db/README.md` wording must not call them included/optional.
- Schema-name scoping (deployment scope + examples), production-SQL has no fixed qualifiers (verified).
- Charter: `README.md` (sections) + `PROGRESS.md`; `PLAN.md` is a redirect stub.
