# viz-consolidation — Progress

## Status

**Slice 1 COMPLETE (2026-07-08), review-verified.** `microflows-viz serve` covers mfinspect
inspect/list parity (`/api/workflow/<id>`, `/api/workflows`), runs read-only as viz_ro, and the
full suite (35/35, incl. the mfinspect parity harness) is wired into the root `test` gate with
`MFVIZ_REQUIRE_DB=1`. Next up: slice 2 (tree / timeline / stuck endpoints).

## Slice tracker

- [x] Slice 1 — COMPLETE (2026-07-08). `microflows-viz serve`: backend package,
      `/api/workflow/<id>` + `/api/workflows` at mfinspect inspect/list parity, static UI
      serving, SELECT-only viz_ro grant (tests run against it), formal parity harness, and the
      suite wired into the root `test` gate with `MFVIZ_REQUIRE_DB=1` (parity harness is
      temporary — replaced by fixture-owned golden tests before slice 4).
- [ ] Slice 2 — `/api/workflow/<id>/tree`, `/timeline`, `/stuck` (derived stuck/waiting verdict
      incl. lease, next_attempt_at, reconcile + redispatch timestamps, resolution_required).
- [ ] Slice 3 — live mode in the browser UI on top of `/api` (demo player stays).
- [ ] Slice 4 — replace parity harness with fixture-owned API tests, then remove
      `microflows/tools/mfinspect/` + `work/mfinspect/`, sweep references.

## Completed steps

- 2026-07-07: effort created; surveyed current state — mfinspect query surface
  (`microflows/tools/mfinspect/src/mfinspect/mfinspect.py`: fetch_workflow/plan/args/operations/
  calls/checkpoints/events + recursive inspect + bounded list), microflows-viz static player
  (self-contained index.html, canned tapes, direct-DB `export_events.py`), and the schema columns
  the stuck endpoint needs (tb_mf_workflow lease/next_attempt_at/disposition; per-op and
  per-checkpoint reconcile_* and redispatch_* fields from the uflowsd-pending-redispatch work).
- 2026-07-07: **slice 1 scaffold/API/grant landed.** New backend package at `microflows-viz/`
  (mariachi/mfinspect-style): `pyproject.toml` (name `microflows-viz`, import package `mfviz`,
  PyMySQL the only dependency), `src/mfviz/{dbq,server,cli}.py`, `tools/build_zipapp.py`,
  `tests/{test_zipapp,test_serve}.py`, `justfile`, committed `./microflows-viz` zipapp.
  - `dbq.py` — mfinspect's connection/decode/fetch helpers + recursive `inspect_workflow`,
    ported essentially unchanged (renames: `MfInspectError`→`MfvizError`; explicit `DbConfig`).
  - `server.py` — stdlib `ThreadingHTTPServer` (accepted decision: no framework). Static
    serving is allowlist-based (`index.html`, `scenarios.js`, `microflows.machine.js`,
    `vendor/`, `plans/`) with three independent guards (normalize + reject `..`; allowlist;
    resolved-prefix check). GET-only; POST/PUT/DELETE → 405. API: `/api/health`,
    `/api/workflow/<32-hex>?max_depth=N` (mfinspect-`inspect`-parity JSON; 404 not_found,
    400 bad max_depth, 502 on DB failure).
  - `cli.py` — `serve` subcommand with `--listen HOST:PORT` (loopback default; non-loopback
    requires `--allow-remote`), `--static-root` (defaults to the executable's directory),
    and the charter's `--db-host/--db-port/--db-user/--db-password/--db-password-env/--db-name`.
  - `microflows/db/grants/viz_ro.sql` — first real grant file: `CREATE USER IF NOT EXISTS
    'viz_ro'` + `GRANT SELECT ON {{SCHEMA}}.*` (mariachi-applied; dev password documented as
    fixture-only, rotate for prod).
  - The traversal test caught a real bug during development (`/vendor/../pyproject.toml`
    passed a naive first-component allowlist and served pyproject.toml) — fixed by
    normalizing before the allowlist check; regression covered with 4 traversal shapes.

## Verification

- `just test` in `microflows-viz/`: **29/29 pass** — 15 packaging tests (incl.
  committed-artifact byte-identity vs fresh build; static UI deliberately NOT bundled in the
  zip), 2 grant tests, 12 serve/API tests. The DB-backed tests skip cleanly when the fixture
  DB (127.0.0.1:34214, `microflows`) is down.
- Grant enforced BY PERMISSION: tests apply the committed `viz_ro.sql` verbatim
  ({{SCHEMA}}-substituted) as root, then prove SELECT works and INSERT/UPDATE/DELETE all fail
  with ER_TABLEACCESS_DENIED (1142) as `viz_ro`; the server test suite runs the backend as
  `viz_ro` throughout.
- End-to-end through the committed zipapp, standalone (no venv/PYTHONPATH), as `viz_ro`:
  `/api/health` 200; `/` serves index.html byte-exact; `/vendor/mermaid.min.js` (3.5 MB) 200;
  `/pyproject.toml` and `--path-as-is /vendor/../pyproject.toml` both 404.
- Parity spot-check: `GET /api/workflow/<id>?max_depth=5` vs
  `mfinspect inspect <id> --max-depth 5` on the same live workflow — **key-sorted JSON
  identical** (the formal seeded-fixture harness is still owed within slice 1).

- 2026-07-07 (review follow-up, 2 findings fixed):
  - README.md now documents the operator entrypoint before this slice is called landed: new
    "Run the live backend (`microflows-viz serve`)" section (viz_ro posture, `--listen` vs
    `--db-*`, `--allow-remote`, current API endpoints, dev workflow) + Files-table rows for
    the backend; `export_events.py` marked as slated for replacement.
  - DB-skip hardening: `tests/test_serve.py` now honors `MFVIZ_REQUIRE_DB=1` — DB/schema
    absence becomes a hard import-time RuntimeError instead of a skip. Verified both ways:
    normal `just test` 29/29 OK; `MFVIZ_REQUIRE_DB=1 DB_PORT=1` → exit 1 with the refusal
    message. The justfile documents that gate wiring MUST export it.
  - Both fixes independently re-verified in review 2026-07-08 (29/29; strict mode exits 1).
- 2026-07-08 (doc cleanup): README's trailing provenance line carried a stale toolchain pin
  (driftc 0.33.63 / ABI 18; certified is 0.33.76 / ABI 20 as of today) — reworded versionless
  (faithful to the coordinator's durable contract as exercised by the root-gate suites) so the
  operator-facing README can't rot against release announcements.

- 2026-07-08 (slice 1 completed):
  - `GET /api/workflows` at mfinspect `list` parity: `script`+`since`+`until` required
    (structural 400 naming the missing params), optional `plan_version`/`state` (code or
    name), summaries ordered created_at DESC with `state_name`. Two documented additive
    fields beyond mfinspect's summary: `href` (`/api/workflow/<id>` — natural link to the
    exact-instance inspection) and `updated_at`.
  - Formal parity harness (`tests/test_parity.py`): seeds a deterministic parent→child tree
    (plan/args/operation/call/checkpoint/events, reserved script name `mfviz-parity`,
    fixed ids/timestamps, FK-safe self-cleanup) as root, serves as viz_ro, and asserts API
    == committed mfinspect zipapp for inspect (full + truncated depth) and list (unfiltered
    + state + plan_version), modulo exactly the two documented additive fields. Plus
    bounded-scan 400s and unknown-state 400. TEMPORARY by charter — to be replaced by
    fixture-owned golden tests before slice 4 removes mfinspect.
  - Root-gate wiring: root `test` now execs `_test-locked` = `_test-combined` (unchanged
    combined drift plan) + `_test-viz`, all under the same DB lock. `_test-viz` resets the
    microflows schema via the component's private `_db-load-schema` (which also applies the
    viz_ro grant, since grants/ is part of the mariachi template) and runs the suite with
    the mariachi venv's python (pymysql; same precedent as `_test-resilience-locked`) with
    **`MFVIZ_REQUIRE_DB=1` exported** — DB absence is a hard failure in the gate. Packaging
    tests skip without a local `.venv` by design (dev-time contract, covered by
    `microflows-viz/just test`).
  - Removed test_serve's opportunistic "inspect whatever row exists" check — it skipped on
    a freshly reset (empty) schema, i.e. a silent data-dependent skip inside the gate; the
    parity harness now owns that assertion deterministically.
  - Verified: `microflows-viz/just test` 35/35 OK; root `just _test-viz` (the exact gate
    path: schema reset + mariachi python + MFVIZ_REQUIRE_DB=1) 35/35 OK, zero skips;
    root justfile parses and `just -n _test-locked` shows the intended chain.
  - README updated: `/api/workflows` documented in the backend section.

## Next literal action

Slice 2: implement `GET /api/workflow/<id>/tree` (skeletal call tree: ids/states/depths
only), `GET /api/workflow/<id>/timeline` (merged event timeline), and
`GET /api/workflow/<id>/stuck` (derived waiting/stuck verdict from lease, next_attempt_at,
reconcile_*/redispatch_* columns, checkpoint resolution_required, and deepest non-terminal
descendant), with fixture-backed tests for each stuck shape per the charter's verification
criteria.
