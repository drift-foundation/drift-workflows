# viz-consolidation — fold mfinspect into microflows-viz

## Short-term objective

Make **microflows-viz** the single operator-facing tool for "what ran?" and "where is this
workflow/job stuck?": a `serve` backend that queries the coordinator MariaDB **read-only** and
feeds the browser UI. Once microflows-viz covers mfinspect's inspect/list use cases, the
standalone `microflows/tools/mfinspect/` package (and its docs/work notes) is removed.

## Current behavior / problem

Two disjoint operator surfaces exist today:

- `microflows/tools/mfinspect/` — CLI zipapp (Python + PyMySQL), two actions:
  `inspect <workflow_id> [--max-depth N]` (full recursive JSON tree: workflow, plan pin + args,
  operations, call sidecar rows, checkpoint stack, full event history, children) and
  `list --script --since --until [--plan-version] [--state]` (bounded search returning a flat
  JSON summary array). JSON-only output; an operator reads raw JSON or pipes to jq. See
  `work/mfinspect/` for its design history.
- `microflows-viz/` — a fully self-contained static demo player (`index.html` + vendored
  Mermaid): sequence/IR-graph/state-machine views animated from **canned scenario tapes**
  (`scenarios.js`), not live data. The only bridge to real runs is `export_events.py`, a manual
  copy-paste step that queries the DB directly from a loose script.

Nobody looking at a stuck production workflow wants either of these alone: the CLI has the data
but no rendering; the viz has the rendering but no data. And "why is this stuck?" is answered by
neither — it currently requires manually walking `tb_mf_call.child_workflow_id` chains and
reading `next_attempt_at` / lease / reconcile / redispatch columns by hand.

## Accepted design decisions

1. **mfinspect is prototype query logic, not a product surface.** Its SQL/JSON-shaping code
   (`src/mfinspect/mfinspect.py`, ~450 lines) seeds the backend's query layer; the CLI itself is
   scheduled for removal, not maintenance.
2. **`microflows-viz serve ...` owns all DB access.** One backend process serves both the static
   UI files and a JSON `/api/...`; DB credentials live only in the backend process. DB flags are
   explicitly **`--db-`-prefixed** (`--db-host/--db-port/--db-user/--db-password/
   --db-password-env/--db-name`) so they can never be confused with the HTTP server's own
   `--listen` — in an operator-facing serve command, a bare `--host`/`--port` must not silently
   mean "database". mfinspect-style env defaults are kept (`DB_HOST/DB_PORT/DB_USER/DB_NAME/
   MDB_ROOT_PWD`), and `--db-password-env` is preferred over `--db-password`, as before.
3. **The browser never queries MariaDB and never holds DB credentials.** The UI talks only to
   `/api/...` on the serving origin. `export_events.py`'s direct-DB path goes away with the rest.
4. **Read-only, always — enforced by DB permissions, not by convention.** Slice 1 ships a
   dedicated SELECT-only DB user (first real occupant of `microflows/db/grants/`), and the
   backend's tests run against that grant, so a mutating statement fails in CI rather than
   surviving as an unnoticed code path. Code-level posture stays the same as mfinspect (no
   claim/resume/notify/unblock/timer-mutation surface exists in the tool at all), and a
   SELECT-only grep remains as a lint — but the grant is the actual guardrail.
5. **Backend API must cover at least:**
   - exact workflow inspection by `workflow_id` (mfinspect `inspect` parity, incl. `max_depth`);
   - bounded search/list (mfinspect `list` parity: script + time range required, no accidental
     full-table scans);
   - call tree / child-workflow tree (`tb_mf_call.child_workflow_id` recursion with the same
     truncation/cycle stubs mfinspect has);
   - timeline of what ran (ordered `tb_mf_workflow_event` view, joinable across a tree);
   - current stuck/waiting reason: active operation/checkpoint/child, plus retry/reclaim/
     redispatch data when available — `tb_mf_workflow.state/current_disposition/
     execution_direction/next_attempt_at/lease_owner/lease_expires_at/current_operation_attempt/
     terminal_reason`, per-op and per-checkpoint `reconcile_attempts/first/last/reason` and
     `redispatch_first_seen_at/redispatch_last_at/redispatch_count` (the
     `work/uflowsd-pending-redispatch` machinery), checkpoint `reversal_state`/
     `resolution_required`.
6. **Removal is part of the charter.** Once parity is verified, delete
   `microflows/tools/mfinspect/` (package, zipapp, docs) and `work/mfinspect/`; sweep references
   (e.g. `work/workflow-composition/*` if still present, top-level docs).
7. **First slice is backend-only API parity** with mfinspect `inspect`/`list`; UI rendering comes
   after the API exists and is verified.
8. **HTTP stack is Python stdlib only.** The backend server uses stdlib HTTP serving primitives
   (`http.server` et al.) for serving and routing — no web framework dependency in slice 1.
   Revisit only if a concrete blocker appears, not as an open design question. PyMySQL stays as
   the DB dependency, matching mfinspect/mariachi — "stdlib only" is about HTTP/server routing,
   not the DB driver.

## Implementation plan

- [ ] **Slice 1 — `serve` + inspect/list API parity (no UI changes).**
  - New backend under `microflows-viz/` (Python, stdlib HTTP serving + PyMySQL as the only
    dependency, packaged mariachi/mfinspect-style — zipapp + justfile + packaging tests),
    subcommand `serve --listen 127.0.0.1:PORT` plus
    DB-prefixed connection flags (`--db-host/--db-port/--db-user/--db-password/
    --db-password-env/--db-name`, env defaults `DB_HOST/DB_PORT/DB_USER/DB_NAME/MDB_ROOT_PWD`).
  - Read-only DB grant: add the SELECT-only viz user under `microflows/db/grants/` and point
    the backend's own tests at it (root/dev credentials stay possible for local convenience,
    but the tested path is the restricted grant).
  - Port mfinspect's fetch/decode/tree logic into the backend's query module.
  - Endpoints: `GET /api/workflow/<id_hex>?max_depth=N` (inspect-parity JSON) and
    `GET /api/workflows?script=&since=&until=[&plan_version=][&state=]` (list-parity JSON;
    script+since+until required → 400 otherwise, mirroring mfinspect's bounded-scan rule).
  - Serves the existing static files (index.html, vendor/, scenarios.js) unchanged.
  - Binds localhost by default; non-localhost bind requires an explicit flag.
- [ ] **Slice 2 — operator-question endpoints.**
  - `GET /api/workflow/<id>/tree` — skeletal call tree (ids/states/depths only, cheap).
  - `GET /api/workflow/<id>/timeline` — merged event timeline (optionally across the tree).
  - `GET /api/workflow/<id>/stuck` — derived "why is this not moving": running-under-lease vs
    scheduled-retry (`next_attempt_at` in the future) vs waiting-on-child (descend calls to the
    deepest non-terminal descendant) vs reconcile-budget/redispatch pending vs
    blocked_resolution/resolution_required vs terminal; includes the raw columns the verdict was
    derived from.
- [ ] **Slice 3 — UI rendering on top of `/api`.**
  - A "live" mode in the viz UI: search form (list), workflow page (tree + timeline + stuck
    verdict), reusing the existing sequence/state-machine rendering where it fits.
  - Demo player (canned tapes) remains functional — live mode is additive.
- [ ] **Slice 4 — retire mfinspect.**
  - Precondition: replace the parity harness with fixture-owned API tests (seeded fixtures +
    golden JSON) so the gate does not depend on the tool being deleted.
  - Parity re-check (verification below) → delete `microflows/tools/mfinspect/` and
    `work/mfinspect/`; sweep repo references; note the successor in microflows-viz README.

## Files likely affected

- `microflows-viz/` — new backend package (src/, tools/, tests/, justfile, pyproject.toml),
  README rewrite, `index.html`/new JS for live mode; `export_events.py` removed (replaced by
  `/api/workflow/<id>/timeline`).
- `microflows/tools/mfinspect/` — removed in slice 4.
- `work/mfinspect/` — removed in slice 4 (its design history is summarized above and in git).
- `microflows/db/grants/` — first real grant file: the SELECT-only viz DB user (slice 1
  requirement).
- Root `justfile` / gates — wherever the new package's tests get wired (pinned means gated:
  a test that no gate runs does not count as verification).

## Verification criteria

- **Parity harness (slice 1 gate, migration-scoped):** against the same seeded DB,
  `mfinspect inspect <id>` / `mfinspect list ...` output and `/api/workflow/<id>` /
  `/api/workflows?...` responses are semantically identical (same rows, same fields; envelope
  differences allowed and documented). Automated as a test the justfile runs, not a manual diff.
  **The parity harness is temporary:** before slice 4 deletes mfinspect, it must be replaced by
  fixture-owned API tests (seeded fixture trees + golden JSON assertions) so the gate stands on
  its own once the reference implementation is gone.
- **Bounded-scan rule enforced structurally:** `/api/workflows` without script/since/until → 400.
- **Read-only proven by permissions:** the backend test suite runs against the SELECT-only grant
  from `microflows/db/grants/` (a mutating statement = test failure at the DB, not a review
  catch); a SELECTs-only grep over the SQL surface stays as a lint; packaging test suite passes
  as for mfinspect/mariachi.
- **No browser→DB path:** UI JS contains no DB host/port/credential handling; page loads and all
  data arrives via `/api` (checked in slice 3 review).
- **Stuck verdicts:** fixture workflows in each stuck shape (scheduled retry, waiting-on-child,
  reconcile pending, redispatch pending, blocked_resolution, running-under-lease) produce the
  expected verdict + timestamps.
- **Slice 4:** repo-wide grep for `mfinspect` returns only historical mentions in commit
  messages / this effort's notes until the folder itself is deleted.

## Open questions / blockers

- **`--tree` CLI ask from work/mfinspect's follow-ups:** does a human-formatted CLI view still
  matter once the browser UI exists, or is `curl /api | jq` enough for headless boxes? Default:
  drop it; revisit if an operator asks.
- **Broader list filters** deferred from mfinspect (`--root-workflow-id`, `--operation-id`,
  `--child-workflow-id`, `--event-kind`, `--terminal-reason`): fold into `/api/workflows` in
  slice 2+ as cheap indexed lookups, or keep deferring — decide when the UI search form is
  designed.

## Relevant review findings

- `work/mfinspect/Progress.md` "Open follow-ups": tree-formatted output, broader list filters,
  log correlation — this effort is where they land or get explicitly dropped.
- `work/mfinspect/Progress.md` verification note: mfinspect's query logic has **no automated
  functional tests** (packaging only, 14/14) — the parity harness above finally closes that gap
  as a side effect.
- `work/uflowsd-pending-redispatch/SPEC.md`: the redispatch escalation columns
  (`redispatch_first_seen_at/last_at/count` on operation + checkpoint rows) are exactly what the
  stuck endpoint must surface for the "crashed participant, awaiting re-PUT/reclaim" story.
