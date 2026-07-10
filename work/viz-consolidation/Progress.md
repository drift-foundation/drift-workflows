# viz-consolidation — Progress

## Status

**ALL FOUR SLICES COMPLETE (slice 1: 2026-07-08; slices 2+3: 2026-07-09; slice 4:
2026-07-10). The charter objective is delivered:** microflows-viz is the single
operator-facing tool — browser live mode (search → stuck verdict → tree → timeline → raw
inspect) over the gated read-only API, and **mfinspect is retired** (package + work notes
deleted; JSON contracts pinned by fixture-owned goldens minted mfinspect-equal at
retirement). 62/62 tests through the root `test` gate with `MFVIZ_REQUIRE_DB=1`.
Remaining: commit the landing; per work/README.md convention this folder is deleted when
the effort lands (commit history becomes the record).

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
- [x] Slice 3 — COMPLETE (2026-07-09). Live mode in the browser UI on top of `/api`
      (demo player stays). Implemented per the plan below; 57/57 through the root gate.
      Plan (2026-07-09, as executed):
      - New self-contained `live.html` (inline CSS/JS, zero deps, no network beyond `/api/*`
        on the serving origin) added to the static allowlist; `index.html` (demo player)
        untouched except an optional header link. Hash routing: `#/` search, `#/wf/<id>`.
      - Search view: form with required script/since/until (datetime-local), optional
        state (dropdown of the 7 names) + plan_version; results table from `/api/workflows`
        (id → detail link via each entry's `href`-derived id, script, state_name,
        disposition, created/updated, terminal_reason). 400s render the API's own `detail`;
        empty result set says so explicitly.
      - Detail view for one id: **stuck verdict first** (verdict pill + `detail` + evidence
        table + path chain links + nested `waiting_on`, and the resolution/redispatch/
        reconcile row lists when present), then the `/tree` skeleton (recursive list, state
        badges, truncation stubs shown), then the `/timeline` (event_ts chronology exactly
        as returned — no client-side reordering, no event_seq anywhere), plus a link to the
        raw `/api/workflow/<id>` inspect JSON.
      - Error surfaces: 404 → "not found" panel; fetch/5xx/502 → error banner carrying the
        API's `detail`; bounded-list 400 → inline form error.
      - Tests (repo pattern = Python unittest over HTTP; no browser engine in the deps):
        live.html served + allowlisted; self-containment lint (no external src/href, no
        DB host/credential strings, fetches only `/api/` paths); no `event_seq` in UI code;
        demo player still served byte-exact.
      - export_events.py: left in place per decision (README already marks `/timeline` as
        its successor).
- [x] Slice 4 — COMPLETE (2026-07-10). Retired mfinspect per the plan below; 59/59
      through the root gate. Plan (2026-07-10, as executed):
      - `tests/test_golden.py` replaces `tests/test_parity.py`: same deterministic seeded
        fixture tree, but asserted against COMMITTED golden JSON files
        (`tests/goldens/*.json`) instead of live mfinspect output. Goldens are minted from
        the API while the parity harness is still green, so their provenance is
        "mfinspect-equal at retirement". Covers: inspect (full + truncated depth), list
        (unfiltered / state / plan_version), and the error surface that lived in the parity
        tests (bounded-scan 400 combos, unknown-state 400) plus 404/bad-depth bounds.
      - Delete `microflows/tools/mfinspect/` (package, zipapp, docs) and `work/mfinspect/`.
        mfinspect is wired into NO gate (verified: no test-plan emitter or justfile runs
        it), so no gate rewiring is needed beyond the viz suite itself.
      - Reference sweep: mfviz code/test/justfile docstrings reworded (successor language,
        no pointers at deleted paths); root justfile gate comments (parity → goldens);
        microflows_design.md §observability + roadmap.md now name microflows-viz as the
        operator tool (mfinspect noted as absorbed); runner.drift comment updated.
        Historical mentions in work/viz-consolidation Progress/README stay (diary/charter).
      - microflows-viz README states it is the successor/operator tool.

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

- 2026-07-09 (slice 3 implemented + verified):
  - `live.html` — one self-contained page (inline CSS/JS, zero dependencies, dark operator
    styling), hash-routed: `#/` search, `#/wf/<32-hex>` detail. All network I/O goes through
    a single `api()` helper fetching same-origin `/api/*` only. Added to the server's static
    allowlist; the demo player gained one "Live mode →" header link and is otherwise
    untouched (byte-exactness asserted in tests).
  - Search view: required script/since/until (datetime-local, defaulting to the last 24 h)
    + optional state/plan_version; results table with state pills and detail links. API 400s
    (incl. the bounded-scan refusal) render the API's own `detail`; empty results say so.
  - Detail view, stuck verdict FIRST: verdict pill + detail + evidence key/values + waiting
    chain links + nested `waiting_on` verdicts + resolution/redispatch/reconcile row tables;
    then the `/tree` skeleton (recursive list, truncation/cycle stubs rendered); then the
    `/timeline` exactly in the API's event_ts chronology (no client-side reordering, no
    event_seq anywhere — negative-asserted); plus a "full inspect JSON ↗" link to
    `/api/workflow/<id>`. 404 → clear not-found panel; backend failure → error banner.
  - Tests (+8 → 57 total): source-level lints (self-containment: zero external URLs, single
    fetch call site, all api() targets under `/api/`; no DB host/port/credential strings; no
    `event_seq`; inspect link present) + served-over-HTTP checks (live.html byte-exact,
    demo player byte-exact with the live link). No browser engine exists in the deps or on
    the host, so rendered-DOM verification is out of scope — the data path was smoke-tested
    end-to-end instead: committed zipapp serving the exact requests the page issues against
    seeded slice-2 fixtures (search incl. state filter, stuck path [P,C,G], tree children,
    timeline order, bounded-scan 400).
  - README quickstart is now browser-first (step 2 = open /live.html; curl flow kept for
    headless boxes); Files table lists live.html. `export_events.py` stays per decision —
    not made dead by this slice (the demo player's tape workflow still references it).
  - Verified: `microflows-viz/just test` 57/57; root `just _test-viz` (schema reset,
    mariachi python, MFVIZ_REQUIRE_DB=1) 57/57, EXIT=0 captured explicitly.

- 2026-07-09 (slice-3 review follow-up, 2 findings fixed):
  - Search bounds now seed from the DATABASE clock, not the browser clock: `/api/health`
    returns `db_now` (NOW(6)); live.html initializes until=db_now / since=db_now−24h using
    Date only for calendar arithmetic on the DB-clock components, leaves the fields blank
    if health is unavailable, and labels both fields "(DB time)" with a note that bounds
    compare against created_at in DB time. Health test pins `db_now`'s presence/shape.
  - test_live_ui's serve tests now mirror ServeTests' posture: grant applied idempotently,
    server created as viz_ro (root creds no longer reach the server), so a future
    accidental API call from that suite cannot hide behind elevated permissions.
  - Re-verified: suite 57/57; root `just _test-viz` 57/57 EXIT=0; `db_now` smoke-checked
    through the committed zipapp.
- 2026-07-10 (slice-3 review follow-up round 2 — DB-time bounds fully backend-owned):
  - `/api/health` now returns SQL-computed `default_since` (NOW(6) − 24h, floored to the
    second) and `default_until` (NOW(6) + 1s, floored — rounded UP past the current
    fractional second so a created_at later in the same second still matches
    `created_at <= until` on DATETIME(6)). live.html copies both verbatim: ALL Date
    arithmetic removed from the page (lint-pinned: no `new Date(`/`Date.now(`), so browser
    timezone/DST normalization can never distort DB-time bounds.
  - Any user-entered until is now inclusive of its whole second too: the query param
    appends `.999999` (sinceParam/untilParam split; since stays a floor).
  - Caught during verification: the first cut doubled `%` in the DATE_FORMAT pattern
    (pymysql only %-interpolates when execute() gets args; without args `%%` reaches SQL
    and DATE_FORMAT emits literal text) — health returned the format string itself. Fixed
    to single `%`; the health-shape test now pins real datetime values (since < db_now <
    until) so this class of regression fails in the gate.
  - Verified: suite 58/58; root `just _test-viz` 58/58 EXIT=0; zipapp health smoke shows
    correct bounds (−24h floor / +1s ceil around db_now).

- 2026-07-10 (slice-3 review round 3, 2 findings fixed): the stale 57/57 in "Verification
  (current)" corrected to 58/58 (dated historical entries left as accurate snapshots); and
  `db_now` is now rendered deterministically at full precision —
  `isoformat(timespec="microseconds")` in both `/api/health` and `/stuck` (plain isoformat()
  drops ".000000" when the fraction is exactly zero) — with the health test pinning
  `\.\d{6}$`.
- 2026-07-10 (**slice 4 executed — mfinspect retired**):
  - Goldens minted (tests/goldens/: inspect_full, inspect_truncated, list_unfiltered,
    list_state_completed, list_plan_version) from the API over the parity fixture tree,
    WITH provenance proof: at mint time each was asserted equal to the committed mfinspect
    zipapp's own output (inspect exact; list modulo the two documented additive fields).
  - `tests/test_golden.py` (8 tests) owns the fixture seeding and asserts the API against
    the committed goldens, plus the error surface inherited from the parity harness
    (bounded-scan 400 combos, unknown-state 400, 404/bad-depth). `tests/test_parity.py`
    deleted.
  - `microflows/tools/mfinspect/` (package + committed zipapp + docs) and `work/mfinspect/`
    deleted. Verified first that no gate/test-plan emitter runs mfinspect — no gate
    rewiring needed.
  - Reference sweep: root justfile gate comments (parity → goldens); mfviz
    __init__/cli/dbq/server docstrings, justfile, test_serve/test_zipapp/build_zipapp
    headers — successor language only, no pointers at deleted paths;
    microflows_design.md §16.5 Observability + §16.6 MVP scope and roadmap.md now name
    microflows-viz as the operator tool; runner.drift correlation comment updated.
    Remaining "mfinspect" mentions are deliberate: retirement/successor statements and this
    effort's own charter/diary.
  - microflows-viz README states it is the successor/operator tool with golden-pinned
    contracts.
  - Verified: suite 59/59 (58 − 7 parity + 8 golden); root `just _test-viz` 59/59 EXIT=0;
    zipapp rebuilt (byte-identity enforced).
- 2026-07-10 (slice-4 review finding fixed — truncation golden was vacuous): with only
  parent→child, max_depth=1 truncates nothing (stubs require depth+1 > max_depth), so
  inspect_full and inspect_truncated were byte-identical. Fixture extended to
  parent→child→grandchild (grandchild completed at depth 2, with its own plan/args/events
  and the child's settled call + sidecar); goldens re-minted (full 9,483 B vs truncated
  7,326 B; list goldens now 3 rows). Structural guards added alongside the golden equality
  so the blind spot cannot recur: the truncated response must contain exactly
  `{"child_workflow_id": <grandchild>, "truncated": true}` and must differ from the full
  golden; the full response must expand the grandchild. Provenance note updated honestly:
  the original two-node goldens were mfinspect-equal at mint; the grandchild extension is
  post-retirement under the same ported truncation semantics. Re-verified: suite 59/59;
  root `just _test-viz` 59/59 EXIT=0.

- 2026-07-10 (consumer-hardening: PushCoin bookkeeper-harness footguns F1/F2 addressed
  pre-publish, per the batch in /tmp/drift-announce/20260710T182452Z-…-footguns.md):
  - **F1 — timestamps are now ISO-8601 UTC with a trailing `Z`** across every endpoint
    (inspect, list, tree/timeline/stuck evidence, operation/checkpoint stamps, db_now).
    DECISION + rationale: coordinator DATETIME(6) values are DB-clock writes, so the `Z` is
    truthful iff the DB runs UTC — verified the fixture does (system tz UTC, NOW(6) ==
    UTC_TIMESTAMP(6)), made "coordinator DB runs UTC" an explicit deployment requirement in
    the README, and added `/api/health.db_utc_offset_seconds` (TIMESTAMPDIFF vs
    UTC_TIMESTAMP, must be 0) as the runtime proof. The DELIBERATE exception:
    `default_since`/`default_until` stay form-shaped (no designator) because the live UI's
    datetime-local inputs cannot hold one — same UTC frame, documented in README, health
    docstring, and the page itself; labels now read "(DB time, UTC)". No browser-clock or
    client date arithmetic reintroduced (lints unchanged). Goldens re-minted (+ a mint-time
    sweep asserting no bare timestamp remains); health test pins the `Z`, the offset==0, and
    the bounds' bare shape.
  - **F2 — checkpoint counters KEPT, authority documented** (removal rejected as unsound):
    the checkpoints[] reconcile_*/redispatch_* fields are NOT duplicates of operations[] —
    they are the compensation(reverse)-side timers (uflowsd-pending-redispatch machinery)
    and are legitimately 0/null on forward-only flows like the reporter's; deleting them
    would gut reverse-direction stuck evidence. Documented the authority split ("read
    forward retry/reclaim/redispatch state from operations[], never checkpoints[]") in the
    README quickstart (before the counters are first mentioned) and dbq's inspect-shape
    docstring.
  - Verified: suite 59/59; root `just _test-viz` 59/59 EXIT=0; zipapp health smoke shows
    `db_now …Z` + `db_utc_offset_seconds: 0` + bare form bounds.
- 2026-07-10 (F1 review round 2, 2 findings fixed — the contract is now ENFORCED and uniform):
  - **Fail closed on a non-UTC DB**: `dbq.connect()` now runs `_assert_db_utc` on every
    connection (the single chokepoint every timestamped payload passes through) and raises
    `DbNotUtcError` — endpoints return a distinct 502 `db_not_utc` config error, health
    included, and `serve` fail-fasts at startup on the same violation (a merely-unreachable
    DB stays non-fatal). `db_utc_offset_seconds` remains the observable but is no longer
    load-bearing: the API can never emit a falsely-Z-designated timestamp. Tested with a
    real `+05:00` session (guard raises) and endpoint-level mocks (all four routes 502
    `db_not_utc`).
  - **Fixed microsecond precision everywhere**: `_decode_value` renders
    `isoformat(timespec="microseconds") + "Z"`, and health/stuck `db_now` now use that same
    single path — zero-microsecond values render `…00.000000Z`, so lexicographic order is
    chronological order within a second (mixed shapes previously sorted "…00Z" after
    "…00.123456Z"). Goldens re-minted; a committed golden test pins every timestamp to
    `\d{6}Z`.
  - Verified: suite 62/62 (59 + 3 new); root `just _test-viz` 62/62 EXIT=0. README contract
    bullet updated (enforced, fail-closed, fixed precision).

## Verification (current)

- `microflows-viz/just test`: **62/62** — 16 packaging tests (incl. committed-artifact
  byte-identity; static UI deliberately NOT bundled in the zip), 14 grant + serve/API tests
  (incl. the non-UTC fail-closed guard + endpoint 502 db_not_utc checks), 9 golden tests
  (fixture-owned inspect/list JSON contracts + error surface + uniform-timestamp pin;
  superseded the parity harness), 14 slice-2 tests (all 8 stuck verdicts + tree/timeline
  shapes), 9 live-UI tests (self-containment + no-browser-clock lints, served-over-HTTP
  checks). DB-backed tests skip only in LOCAL runs without the
  fixture DB; the root gate exports `MFVIZ_REQUIRE_DB=1`, making absence a hard failure.
- Read-only enforced BY PERMISSION: the suites apply the committed `viz_ro.sql` verbatim
  ({{SCHEMA}}-substituted) as root, prove INSERT/UPDATE/DELETE all fail with
  ER_TABLEACCESS_DENIED (1142) as `viz_ro`, and run every server test as `viz_ro`.
- Fixture-owned goldens (tests/goldens/*.json) pin the inspect + list JSON contracts;
  provenance: minted from the API on 2026-07-10 while the parity harness was still green,
  each asserted equal to mfinspect's own output at mint time. A deliberate contract change
  means re-minting the goldens and reviewing the diff.
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

Land the effort: commit the slice-3 review fixes + slice 4 (goldens, deletions, reference
sweep), then — per work/README.md ("when an effort LANDS, delete its folder") — remove
`work/viz-consolidation/` in the landing commit or immediately after; the commit history is
the durable record.
