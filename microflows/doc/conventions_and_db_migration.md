# Conventions & DB Migration — report for confirmation

**Status:** report for confirmation (2026-06-08)
**Purpose:** record the concrete **Drift Foundation** repository conventions and
the **Mariachi** database-artifact protocol we must adopt, and propose a
migration plan. **No database files or signing/packaging artifacts are
restructured until this is confirmed** (per direction: "Before restructuring
database files, report the concrete Mariachi layout and migration steps").

References inspected: `../mariachi` (DB-artifact authority), `../drift-web` and
`../drift-mariadb-client` (Foundation convention authority),
`../pushcoin/bookkeeper` and `../pushcoin/singular` (consumer practice).

---

## 0. Umbrella identity, ownership & packaging boundary (decided)

The umbrella project/repository identity is **Drift Workflows**
(`drift-workflows`), a Drift Foundation project with two distinct components:

```text
Drift Workflows
├── Microflows — the durable workflow manager/SERVICE + runtime we build.
│               Product/runtime name throughout its package, service,
│               protocol, and design docs. Implemented in THIS tree
│               (packages/microflows). Other teams use the service.
└── Singular   — a reusable Drift idempotency LIBRARY (peer of drift-web /
                mariadb-client), independently versioned, consumed by
                PARTICIPANT services and other teams. Microflows does NOT
                depend on Singular.
```

Both are ours and may eventually share this repository, but **nothing is moved
now**: Singular remains at `../pushcoin/singular` and is consumed as an
external Drift package; we make no speculative relocation. Architectural and
package boundaries stay explicit even after any future co-location. Relocation
happens only once repo layout, package publication, and a migration plan are
ready.

The repository directory is historically named `phase-drift`; that is cosmetic
— the project identity is **Drift Workflows** and the runtime is **Microflows**.

### Foundation signing / publication planning

The repo-ROOT `drift/manifest.json` is the **sole release + signing surface** for the shipped artifacts
— `singular`, `microflows`, and `uflowsd` — signed under the **Drift Foundation** key
(`the-drift-foundation.author-profile`; `DRIFT_SIGN_KEY_FILE` default `~/.config/drift/keys/default.seed`;
author-claims via `just author-claim`, lock + trust via `just reseal`, publish via `just deploy`).

The per-component manifests (`microflows/drift`, `singular/drift/drift`, `microflows/runner/drift`,
`microflows/participant-stub/drift`) are **local-dev only**: every artifact is version `0.0.0` and carries
**no `author_profile`**, so an accidental `drift deploy` from a component tree fails closed. **Do not add
`author_profile` (or a real version) to a component manifest — release is root-only.**

`singular` now lives under Drift Workflows and is a **root-released, Foundation-signed artifact** (0.7.0) —
it is no longer an external / PushCoin-signed dependency.

---

## 1. Drift Foundation conventions to adopt

Microflows is a Drift Foundation project. Where current Foundation patterns
(`drift-web`, `drift-mariadb-client`) differ from the PushCoin/Singular
template we bootstrapped from, **prefer Foundation**.

Concrete diffs from our current (Singular-derived) setup:

| Area | Current (Singular-derived) | Foundation convention |
|---|---|---|
| `manifest.author_profile` | `pushcoin.author-profile` | `the-drift-foundation.author-profile` |
| Signing key | PushCoin key `ed25519:Yvjb…` | Foundation key `ed25519:6DSI…` (`DRIFT_SIGN_KEY_FILE`, default `~/.config/drift/keys/default.seed`) |
| `trust.json` | Foundation + PushCoin keys; `microflows.*`→PushCoin key | Foundation key only; `microflows.*`→Foundation key |
| Author-claim tool | `drift author` (old CLI) | `PYTHONPATH=$DRIFT_LANG_ROOT python3 -m tools.drift_author publish` |
| Test gates | `test` only | `test` **+ `stress` + `perf`** (each via `emit_test_plan.py {test,stress,perf}` + shared executor) |
| `emit_test_plan.py` | `test/one/compile` | add `stress` and `perf` subcommands |
| Test layout | `packages/<a>/tests/{unit,e2e}` | same, **+ `tests/stress/`, `perf/scenarios/`, optional `tests/spike/`** |
| `assets` in manifest | `[]` | `docs/integration-guide.md`, `README.md` (+ `effective-*.md`) |
| `.gitignore` | `tmp_db_instances/` only | + `build/`, `.claude-session`, `.codex*`, `perf/{captures,results}/`, db temp dirs |
| Deploy | custom `cert`+`deploy` | root `just deploy` — **multi-artifact** (singular + microflows packages + the uflowsd **app**, `--app-dest`); cert-suite is **orchestrator-owned** for staging; `just deploy` adds a dev-only fallback locally, suppressed when `--cert-suite*` or `DRIFT_DEPLOY_CERT_SUITE_*` is supplied |
| Docs | none | `docs/integration-guide.md` required; `AGENTS.md` (already standard) |

Notes:
- `author_profile`, signing, the lock, and `drift deploy` are **ROOT-only** (top-level `drift/manifest.json`); the `pushcoin → foundation` author_profile row above refers to the ROOT. Per-component manifests are local-dev (`0.0.0`, no `author_profile`).
- Foundation repos are **multi-package** (`packages/<libA>`, `<libB>` under one
  manifest with an `artifacts[]` entry each). The root `drift/manifest.json` now declares THREE
  artifacts — `singular` + `microflows` (packages) and `uflowsd` (**app**) — the Foundation
  multi-package shape; the `packages/<pkg>/` layout fits it.
- Foundation **commits `drift/lock.json`** and does **not** generate a
  `version.drift`; version lives only in the manifest.

**Confirmed + landed (2026-06-08):**
1. Signing identity switched to the **Foundation key**
   (`the-drift-foundation.author-profile`; the root artifacts span namespaces `singular`,
   `microflows.*`, and `microflows.runner.*` (uflowsd); `trust.json` Foundation-key-only;
   `author-claim` recipe uses `tools.drift_author` + `DRIFT_SIGN_KEY_FILE`).
   `pushcoin.author-profile` removed. Deps still resolve; build/test green.
2. **`stress` and `perf` gates** added as empty Foundation-standard stubs
   (`just stress` / `just perf` print "no scenarios yet") so certification has
   the recipes; populate as scenarios land.

---

## 2. Mariachi DB-artifact protocol (the canonical layout)

Mariachi is the Foundation **template-driven MariaDB schema orchestrator**
(declarative desired-state; idempotent `plan`/`apply`; environment-stamped
dev/prod protection). It is a sibling repo invoked as a venv CLI
(`../mariachi/.venv/bin/mariachi`), **not** a code dependency.

### 2.1 Required consumer directory layout

```text
db/
├── schema/      tb_<name>.sql   — CREATE TABLE IF NOT EXISTS; FKs MUST be named
├── procs/       sp_<name>.sql   — CREATE [OR REPLACE] PROCEDURE; DEFINER stripped
├── constants/   <table>.data.csv — seed rows (hex for binary cols); optional
├── grants/      *.sql           — CREATE USER / GRANT, {{SCHEMA}} placeholder
└── scenarios/   <name>/<table>.data.csv — dev-only data overlays; optional
```

All four canonical dirs should exist (even if empty). Mariachi applies in
order schema → procs → constants → grants, and handles its own ordering. It
keeps one internal `__tb_mariachi_meta__` table per managed schema (the
immutable `development`/`production` marker) — never in the template.

### 2.2 Deploy + test workflow (replaces the ad-hoc bash loader)

```bash
# one-time:  cd ../mariachi && just setup   (creates .venv/bin/mariachi)

# dev reset (drop + recreate + apply full template):
mariachi --schema-template db --host 127.0.0.1 --port 34114 \
  --user root --password-env MDB_ROOT_PWD \
  apply --schema microflows --env=development --allow-destructive --destroy-database

# plan a diff (no mutation):
mariachi ... plan --schema microflows
```

SP behavior is tested as Bookkeeper/Singular do: load the schema via Mariachi,
then drive SPs over `mariadb-rpc` (Drift e2e) or pymysql (Python), asserting
the discriminated-JSON `outcome` contract — explicit IDs/timestamps in,
deterministic state-idempotent transitions out.

---

## 3. Migration plan for our current `db/`

Our current `db/` is a flat `schema/` + `procs/` loaded by an ad-hoc
`just db-load-schema` bash loop. Conversion to Mariachi form:

**Compatible already:**
- File-per-resource naming `tb_mf_*.sql` / `sp_mf_*.sql` ✓
- FKs are **named** (`CONSTRAINT fk_… FOREIGN KEY …`) ✓ — but use the stale
  `fk_pd_` prefix; rename to `fk_mf_` for consistency.
- SPs are `CREATE PROCEDURE` with `DELIMITER $$` ✓

**Changes required:**
1. `CREATE TABLE` → `CREATE TABLE IF NOT EXISTS` in all three `tb_mf_*.sql`.
2. Rename FK constraints `fk_pd_*` → `fk_mf_*` (2 occurrences).
3. Add `db/constants/` and `db/grants/` (empty for now; no seed/grants yet) and
   `db/scenarios/` (for future dev fixtures).
4. Replace the `just db-load-schema` bash loop with a Mariachi `apply`
   invocation (schema name `microflows`, `--env=development`). Keep `db-sql`
   for ad-hoc queries; drop the C-locale glob-ordering hack (Mariachi orders).
5. Add a `MARIACHI` justfile var (`env("MARIACHI_BIN", "../mariachi/.venv/bin/mariachi")`)
   and a setup precondition check, mirroring Bookkeeper.
6. SP discipline already matches the requirement (explicit IDs/timestamps, no
   ambient clock, deterministic state-idempotent transitions, §24.4).

**Out of scope of this migration (unchanged):** the runtime never issues free
SQL; all coordinator access is via these purpose-built SPs over `mariadb-rpc`.
The operation request/result tables (step 2b) will be authored directly in
Mariachi form once the spike fixes their shape.

**Confirmation needed before I touch DB files:**
- Schema name `microflows` (database/schema) — OK?
- Proceed with steps 1–6 above as the DB restructuring?

---

## 3a. Singular multi-language layout (PROPOSAL — confirm before moving)

Singular is a **language-neutral idempotency protocol + library ecosystem**;
the Drift binding is its first reference implementation, not the definition.
The final `singular/` component must leave room for independently versioned
language bindings, all converging on one shared authoritative backend.

**Precedent check:** no Foundation repo today is multi-language — `drift-web`,
`drift-mariadb-client`, `drift-net-tls` are all single-language (Drift),
multi-*package*, with package metadata in `drift/manifest.json` and source in
`packages/`. So the language-subtree layout is novel; the risk is breaking
Drift package tooling, which expects `drift/manifest.json` **relative to the
project root (CWD)**.

**Proposed layout:**

```text
singular/
├── doc/
│   ├── singular-protocol.md      # normative, language-neutral (written)
│   └── ...
├── db/                           # shared MariaDB backend: schema + SPs
│                                 #   (the reference backend, Part II of the spec)
├── tests/
│   └── conformance/              # language-neutral protocol vectors (§12 of spec)
├── drift/                        # the Drift binding — a self-contained project
│   ├── drift/manifest.json       # Drift package metadata (convention: drift/ dir)
│   ├── packages/singular/src/    # Drift source
│   ├── justfile
│   └── ...
├── java/    (future; not created until implemented)
├── rust/    (future)
├── python/  (future)
└── justfile                      # umbrella: delegates build/test to each binding,
                                  #   loads db/, runs conformance
```

**The tooling tension + recommendation.** Drift's `prepare`/`build` look for
`drift/manifest.json` relative to CWD. Two ways to host the Drift binding under
`singular/drift/`:

```text
(A) RECOMMENDED — language subtree is a normal Drift project:
      singular/drift/drift/manifest.json + singular/drift/packages/singular/
    Run Drift tooling with CWD = singular/drift/. Convention unchanged; Drift
    tooling works as-is. The "drift/drift/" nesting is cosmetic, not a break.

(B) Flatten: singular/drift/manifest.json + singular/drift/packages/singular/
    (no inner drift/ dir). This BREAKS `prepare`/`build` unless drift supports a
    --manifest override for prepare. Needs a toolchain confirmation before use.
```

Recommendation: **(A)** — it keeps Drift tooling working with zero changes and
keeps each binding self-contained (its own manifest/lock/trust/justfile). The
component-root `singular/justfile` delegates to `singular/drift/` (and future
bindings), applies `singular/db/` via Mariachi, and runs the shared conformance
suite.

**Boundaries (decided in direction):**

```text
singular/db/                — defines the shared backend protocol/schema.
each language binding        — implements the SAME public Singular semantics
                              (the protocol spec), converging on the same logical
                              keys / state machine / leases / fencing / outcomes.
binding-specific manifests/builds/tests/metadata — stay inside that language
                              subtree (singular/<lang>/).
singular/tests/conformance/  — cross-language vectors; every binding proves
                              equivalence against the same backend.
java/rust/python/            — documented now, created when implemented.
```

**Before moving files:** confirm layout (A) vs (B), and whether to reconcile
the in-repo `singular/` copy with `../pushcoin/singular` (the external dep the
Drift binding builds against today). No Singular files are moved until
confirmed (per direction).

## 3b. Repository consolidation (PROPOSAL — bundled with the Singular move)

The umbrella `drift-workflows` monorepo target:

```text
drift-workflows/                 (dir currently named phase-drift)
├── README.md                    # umbrella (done)
├── .gitignore                   # umbrella (done)
├── justfile                     # thin: delegates test/stress/perf to components
├── singular/                    # the Singular component (§3a layout)
└── microflows/                  # the Microflows component:
    ├── packages/microflows/     # (moves from repo-root packages/)
    ├── participant-stub/        # (moves from repo-root)
    ├── drift/ db/ doc/ tools/   # (move from repo-root)
    ├── doc/history/java-microflows/  # the existing microflows/doc/ prior-art
    └── justfile
```

Path updates required after the move (all mechanical): participant-stub
`_pkg-local` (→ in-repo `../../singular/<binding>/…/dist/lib` once Singular's
layout is set), README doc links (`doc/…` → `microflows/doc/…`), `.gitignore`
prefixes, and a thin root justfile. **Microflows consolidation is unambiguous
and can execute independently;** it is bundled here only so the root
justfile/gitignore are written once, after the Singular layout is confirmed.

## 4. Status

**Done (2026-06-08):** Mariachi migration (steps 1–6: `IF NOT EXISTS`,
`fk_mf_*` rename, `constants/grants/scenarios` dirs, `mariachi apply` replaces
the bash loader, `MARIACHI` var, glob hack dropped); Foundation signing
identity swap; `stress`/`perf` gate stubs; Foundation `.gitignore`. Schema
loads via `mariachi apply`; full gate green.

**Still NOT done without further confirmation:** moving/relocating Singular or
changing the external Singular dependency.
