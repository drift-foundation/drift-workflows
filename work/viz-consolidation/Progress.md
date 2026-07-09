# viz-consolidation — Progress

## Status

**Slices 1 and 2 COMPLETE (slice 1: 2026-07-08 review-verified; slice 2: 2026-07-09).**
The full operator API surface required before any team-facing release is now present AND gated:
search/list (`/api/workflows`), raw inspect (`/api/workflow/<id>`, mfinspect parity), call
**tree**, event **timeline**, and the derived **stuck** verdict — all read-only as viz_ro,
stdlib HTTP only, 49/49 tests through the root `test` gate with `MFVIZ_REQUIRE_DB=1`.
Next up: slice 3 (browser UI live mode on top of /api).

## Slice tracker

- [x] Slice 1 — COMPLETE (2026-07-08). `microflows-viz serve`: backend package,
      `/api/workflow/<id>` + `/api/workflows` at mfinspect inspect/list parity, static UI
      serving, SELECT-only viz_ro grant (tests run against it), formal parity harness, and the
      suite wired into the root `test` gate with `MFVIZ_REQUIRE_DB=1` (parity harness is
      temporary — replaced by fixture-owned golden tests before slice 4).
- [x] Slice 2 — COMPLETE (2026-07-09). `/api/workflow/<id>/tree`, `/timeline`, `/stuck`
      implemented per the plan below, fixture-backed tests for every stuck verdict, 49/49
      through the root gate. Plan (2026-07-09, as executed):
      - `tree`: skeletal recursion over tb_mf_call — per node workflow_id, parent_workflow_id,
        depth, script_name, state/state_name, current_disposition, terminal_reason, children;
        same max_depth/truncated/cycle stubs as inspect. Cheap: two narrow SELECTs per node.
      - `timeline`: walk the same tree skeleton, then one events SELECT over all collected ids,
        ORDER BY event_ts, workflow_id (event_ts is the workflow-local chronological key,
        strictly monotonic per workflow; workflow_id is an internal cross-workflow tie-breaker);
        each entry annotated with workflow_id/depth/script_name.
      - `stuck`: verdict + evidence, classified in precedence order: terminal >
        blocked_resolution (checkpoint reversal_state=3 evidence) > running_under_lease
        (lease_owner set, lease_expires_at > db_now) > waiting_on_child (any non-terminal
        child; descend recursively to the deepest non-terminal descendant, path recorded) >
        redispatch_pending (op/ckpt redispatch_* set — the escalation, so it outranks) >
        reconcile_pending (op/ckpt reconcile_* set on a pending row) > scheduled_retry
        (next_attempt_at > db_now) > claimable_now. All time comparisons against DB NOW(6),
        returned as `db_now` in the response. Evidence carries the raw columns used.
      - Fixture-backed tests: one seeded workflow per stuck shape (all 8 verdicts) + tree
        shape/truncation + timeline ordering, in the reserved `mfviz-slice2` script namespace,
        FK-safe self-cleanup, run as viz_ro through the HTTP surface.
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

- 2026-07-09 (slice 2 implemented + verified, endpoint by endpoint):
  - `/tree` — skeletal recursion in `dbq.tree_workflow` (two narrow SELECTs per node — the
    skeleton projection + child ids from tb_mf_call), per-node workflow_id /
    parent_workflow_id / depth / script_name / state / state_name / current_disposition /
    terminal_reason / children; inspect's max_depth/truncated/cycle semantics preserved.
    Verified: nesting + fields + depths over a seeded P→(terminal TC, C→G) tree, truncation
    stub at max_depth=1, 404 unknown id, 400 bad max_depth. No payload keys in the response.
  - `/timeline` — flattens the tree skeleton to an id→{depth, script_name} map, then ONE
    events SELECT over all ids, ORDER BY event_ts, workflow_id; each entry
    annotated with workflow_id/depth/script_name; response carries the id map. Verified:
    cross-workflow merge order (P@T0, C@T1, G@T2, P@T3), depth annotations 0/1/2/0, payload
    decode, and max_depth=1 excluding the grandchild's node + events.
  - `/stuck` — `dbq.stuck_workflow` verdict + evidence, precedence: terminal >
    blocked_resolution > running_under_lease > waiting_on_child (recursive descent, path
    recorded) > redispatch_pending > reconcile_pending > scheduled_retry > claimable_now.
    Evidence = raw columns used (state/disposition/direction, terminal_reason, lease
    owner/expiry, next_attempt_at, attempt counter, updated_at) + attention rows (pending /
    reconcile / redispatch ops and non-forward-held checkpoints) + `db_now` (DB clock, used
    for every comparison). Verified with one seeded workflow per verdict — all 8 — including
    redispatch outranking a simultaneously-set reconcile budget, and descent skipping a
    terminal sibling to reach the deepest non-terminal descendant (path [P, C, G], nested
    verdicts waiting_on_child → waiting_on_child → claimable_now).
  - Suite: 49/49 standalone AND via root `just _test-viz` (schema reset, mariachi python,
    MFVIZ_REQUIRE_DB=1); zipapp rebuilt (byte-identity enforced). README: quickstart step 5
    (tree/timeline/stuck curl examples + verdict glossary) and the API summary updated; the
    "slice 2 will add" scope note removed. Release precondition met: list/search + inspect +
    tree + timeline + stuck all present and gated.

- 2026-07-09 (design change folded into the slice-2 landing: **event_seq removed as an
  ordering concept** — event_ts is the workflow-local chronological key; pre-production, so
  storage + API cleaned in place rather than migrated):
  - Schema: `tb_mf_workflow_event.event_seq` dropped, PK now `(workflow_id, event_ts)`;
    `tb_mf_workflow.current_event_seq` dropped (`current_event_ts` stays as the latest-event
    pointer AND the append monotonicity fence); checkpoint `resolution_event_seq` →
    `resolution_event_ts`.
  - All 16 event-appending SPs reworked: no seq derivation, event INSERT without seq,
    `current_event_ts` advance only; the existing `arg_event_ts > current_event_ts` guard
    (event_time_skew + defer_until, runner retries with a later DB-clock ts) is the ONLY
    ordering mechanism — no second fallback. Defer-family SPs keep their fold-to-no-append
    skew behavior. Readers: dispatch_defer `ORDER BY event_ts DESC`; plan_stalled joins the
    latest event via `current_event_ts`; claim SPs no longer project a seq.
  - Runtime: `ClaimedWorkflow.current_event_seq` removed (host.drift, both parse sites);
    live_lease_test assertion dropped.
  - Tests/tools: sp_call_test (seed helpers, direct INSERTs, assertions now select by kind /
    order by event_ts / check current_event_ts), sp_operation_test, seed proc, integration
    test.py, export_events.py, migration 0001 backfill query — all seq-free. mfinspect +
    mfviz inspect order events by event_ts; `/timeline` responses carry NO event_seq (order
    is event_ts chronology; workflow_id is an internal equal-ts tie-breaker only), with a
    negative assertion pinning that. Both zipapps rebuilt. Design docs
    (microflows_design/phase_drift_mile_design/storage_portability) updated — I2 is now
    "event_ts chronology is the only event order".
  - The first full-gate run caught two missed fixture surfaces (the sweep's grep excluded
    .csv): the coordinator-fixtures scenario CSVs still carried the seq columns AND had
    same-timestamp event rows that collide on the new `(workflow_id, event_ts)` PK — fixed
    by dropping the columns, moving wf 01/02/04's second event to `.000001`, and advancing
    those rows' `current_event_ts`/`updated_at` to stay head-consistent (plan_stalled now
    joins the latest event via `current_event_ts`). The integration-harness failure was
    downstream (FK on the unloaded fixture row). Also a lesson re-learned: piping a gate to
    `tail` masks its exit code — the first background run "completed (exit 0)" while the
    gate inside had failed; reruns capture `EXIT=$?` explicitly.
  - Verified: schema + all procs apply cleanly via mariachi; SP regressions **156/156 +
    131/131**; viz suite 49/49; full root `just test` GREEN end-to-end (combined drift plan
    61 jobs incl. rebuilt host.drift, e2e, coordinator-singular integration; then the viz
    gate 49/49; EXIT=0).

## Verification (current)

- `microflows-viz/just test`: **49/49** — 16 packaging tests (incl. committed-artifact
  byte-identity; static UI deliberately NOT bundled in the zip), 12 grant + serve/API tests,
  7 mfinspect-parity tests (seeded fixture tree), 14 slice-2 tests (all 8 stuck verdicts +
  tree/timeline shapes). DB-backed tests skip only in LOCAL runs without the
  fixture DB; the root gate exports `MFVIZ_REQUIRE_DB=1`, making absence a hard failure.
- Read-only enforced BY PERMISSION: the suites apply the committed `viz_ro.sql` verbatim
  ({{SCHEMA}}-substituted) as root, prove INSERT/UPDATE/DELETE all fail with
  ER_TABLEACCESS_DENIED (1142) as `viz_ro`, and run every server test as `viz_ro`.
- Formal parity harness (slice 1) supersedes the early live-data spot-check: API ==
  committed mfinspect zipapp on a seeded deterministic tree, inspect (full + truncated) and
  list (unfiltered/state/plan_version), modulo the two documented additive fields.
- Repo gate: root `just test` = combined drift plan (61 jobs) + `_test-viz` under one DB
  lock — last full run GREEN end-to-end (EXIT=0) on certified driftc 0.33.77, including the
  event_ts redesign (SP regressions 156/156 + 131/131 inside the plan).

## Review-history log

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

Slice 3: live mode in the browser UI on top of `/api` — a search form (`/api/workflows`),
a workflow page (tree + timeline + stuck verdict), reusing the existing sequence/state-machine
rendering where it fits; the canned-tape demo player stays functional. Before starting, decide
whether `export_events.py` is retired in this slice (its replacement, `/timeline`, now exists).
