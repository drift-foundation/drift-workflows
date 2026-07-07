# viz-consolidation — Progress

## Status

**Not started — charter written 2026-07-07.** No code yet; `work/viz-consolidation/README.md`
records the agreed scope, design decisions (serve backend owns DB access, browser never touches
MariaDB, mfinspect is prototype logic slated for removal), the four-slice plan, and the
verification criteria.

## Slice tracker

- [ ] Slice 1 — `microflows-viz serve`: backend package + `/api/workflow/<id>` +
      `/api/workflows` at mfinspect inspect/list parity, static UI serving, SELECT-only DB
      grant in `microflows/db/grants/` (tests run against it), parity harness wired into a
      gate (temporary — replaced by fixture-owned golden tests before slice 4).
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

## Verification

Nothing to verify yet. Gate-worthy checks are defined in README.md "Verification criteria"
(parity harness vs mfinspect on a seeded DB, structural bounded-scan 400, SELECT-only assertion,
stuck-verdict fixtures, no-browser-DB-path review).

## Next literal action

Start slice 1: scaffold the backend package under `microflows-viz/` (mariachi/mfinspect-style:
`pyproject.toml`, `src/<pkg>/`, `tools/build_zipapp.py`, `tests/test_zipapp.py`, `justfile`),
porting `mfinspect.py`'s connection/decode/fetch helpers into a query module (connection flags
renamed to the charter's `--db-*` form), and add the `serve` subcommand (stdlib HTTP serving
primitives only — accepted decision, no framework) serving static files +
`GET /api/workflow/<id_hex>?max_depth=N` first. In the same slice: the SELECT-only grant file
under `microflows/db/grants/` and tests pointed at it. No open questions block the start.
