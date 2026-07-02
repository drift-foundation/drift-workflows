# mfinspect — Progress

## First slice — LANDED

Built `microflows/tools/mfinspect/` per the scope agreed in
`work/workflow-composition/PROGRESS.md` ("mfinspect first, then 1c compensation"), through several
rounds of review that shaped the final packaging and CLI shape below. An initial draft shipped as a
single flat ambient script (`microflows/tools/mfinspect.py`, one mode: dump one `workflow_id`); it
was replaced entirely by the package described here — the flat script no longer exists.

### Packaging (mariachi-style zipapp, not an ambient script)

Reviewed explicitly: "package it as a self-contained zipapp like Mariachi... don't leave it as an
ambient-Python script depending on whichever venv happens to be active." Mirrored
[`mariachi`](../../../mariachi) directly, file-for-file:
- `microflows/tools/mfinspect/pyproject.toml` — `PyMySQL>=1.1.0,<2.0` dependency pin, console
  script `mfinspect = "mfinspect.mfinspect:main"`.
- `microflows/tools/mfinspect/src/mfinspect/mfinspect.py` — the implementation; `main(argv) -> int`
  / `run() -> None: sys.exit(main())` split (mirrors `mariachi.py`'s own split — `main` is what the
  installed console-script wrapper and unit tests call directly; `run` is what the zipapp's
  `__main__.py` calls, since a bare script invocation needs its own `sys.exit`).
- `microflows/tools/mfinspect/tools/build_zipapp.py` — copied near-verbatim from
  `../mariachi/tools/build_zipapp.py` (renamed `mariachi`->`mfinspect` throughout): bundles the
  package source + the PyMySQL dependency (with its MIT license + a minimal dist-info) + a
  synthetic `mfinspect-<version>.dist-info` (so `importlib.metadata.version("mfinspect")` — and
  therefore `--version` — resolves inside the zip) into one `#!/usr/bin/env python3` + ZIP archive.
  Reproducible by construction: sorted archive entries, a fixed 1980-01-01 ZIP timestamp, fixed
  `0o644` file permissions — two builds of identical inputs are SHA-256 identical. Stages into a
  temp dir first and `os.replace()`s atomically at the end, so a failure mid-build never touches an
  existing artifact and never leaves a half-written temp file behind.
- `microflows/tools/mfinspect/tests/test_zipapp.py` — copied near-verbatim from
  `../mariachi/tests/test_zipapp.py`: proves the artifact has the right shebang, reports the
  `pyproject.toml` version, runs `--help` standalone, runs with `PYTHONPATH`/`VIRTUAL_ENV`
  stripped from the environment, bundles PyMySQL (importable straight out of the zip) + its MIT
  license + dist-info METADATA, excludes `__pycache__`/`.pyc`/`tests/` from the archive, is
  byte-for-byte reproducible across two fresh builds, replaces a stale/unrelated file cleanly on
  rebuild, and — via mocking `_write_archive` to fail — proves a failed build leaves any existing
  committed artifact byte-for-byte untouched and leaves no orphaned temp file. **14/14 pass.**
- `microflows/tools/mfinspect/justfile` — `setup` (venv + editable install), `build` (zipapp),
  `test` (the packaging suite above), `inspect`/`list` (thin pass-through to the built console
  script), `help`, `clean` — mirrors `../mariachi/justfile`'s recipe shape.
- Built and committed `microflows/tools/mfinspect/mfinspect` (the zipapp itself).

Verified end to end outside any venv (`unset PYTHONPATH VIRTUAL_ENV`): `--version`, `--help`,
`inspect --help`, `list --help` all run standalone from the bare executable; `inspect`/`list` both
produce correct JSON against the live DB through the built zipapp (not just the editable install).

### CLI shape (converged through several rounds of review)

Two actions, because a script/`.mf` name is not an instance identity (many workflow instances can
run the same script) — the final agreed shape narrowed a broader `list` filter set down to a small,
deliberately-bounded first slice:

- **`mfinspect [global args] inspect <workflow_id> [--max-depth N]`** — exact-instance mode. No
  `--since`/`--until` (an explicit correction mid-review: "it is an exact workflow-instance dump, so
  it should include the full durable state/events for that workflow tree" — unfiltered). `workflow`,
  `plan`, `args`, `operations`, `calls`, `checkpoints`, the FULL `events` history, and `children`
  (recursing through every `tb_mf_call.child_workflow_id` up to `--max-depth`, matching
  `tb_mf_workflow.call_depth`'s own numbering — `--max-depth N` expands `call_depth <= N` fully;
  beyond that, a child becomes an explicit `{"child_workflow_id":..., "truncated": true}` stub,
  never silently omitted; a defensive `seen`-set also stubs `{"cycle_detected": true}` if the DB is
  ever corrupted into a cycle — belt-and-suspenders for the inspector itself, since the runtime's own
  ancestor-set + `max_call_depth` guard is what actually prevents this in a healthy system).
- **`mfinspect [global args] list --script NAME --since TS --until TS [--plan-version V] [--state S]`**
  — search/discovery mode. `--script`/`--since`/`--until` are all REQUIRED, deliberately, "to rule
  out an accidental full-table scan" in production. `--since`/`--until` filter
  `tb_mf_workflow.created_at` (written in the same transaction as the workflow's first "created"
  event, so it doubles as "first event timestamp" without an extra join). `--state` accepts either a
  numeric code or a name (`forward`/`reversing`/`blocked_resolution`/`completed`/`reversed`/
  `resolved_exception`/`failed`). Output is a **bare JSON array** of summaries — `workflow_id`,
  `script_name`, `plan_version` (via a `LEFT JOIN tb_mf_workflow_plan`, null for a legacy
  single-op workflow), `state`/`state_name`/`execution_direction`/`current_disposition`,
  `parent_workflow_id`/`root_workflow_id`, `created_at`, `current_event_ts` (latest event
  timestamp), `terminal_reason` — never a tree. An operator picks a `workflow_id` from the results,
  then `inspect`s it.
- **Global DB/output args** (before the action, e.g. `mfinspect --host ... --port ... list ...` —
  matches `mariachi`'s own convention of placing connection flags on the top-level parser before its
  subcommands): `--host` (default `$DB_HOST` or `127.0.0.1`), `--port` (`$DB_PORT` or `34214`),
  `--user` (`$DB_USER` or `root`), `--password` (direct override, mostly for local dev), `--password-env`
  (default `MDB_ROOT_PWD` — preferred over `--password` so a password never lands in shell history
  or a process listing), `--database` (`$DB_NAME` or `microflows`), `--indent` (0 for compact JSON).

### Review rounds that shaped this (chronological)

1. First draft: a single flat `microflows/tools/mfinspect.py` script, one mode (`workflow_id ->`
   full tree), env-var-only DB connection, ambient-venv execution.
2. **"Package it like mariachi, not driftc."** Rebuilt as the zipapp package described above —
   `driftc`'s PEX/scie packaging embeds Python + compiler deps and doesn't fit a small DB tool;
   `mariachi`'s zipapp (source + PyMySQL only) is the right precedent.
3. **"DB connection should be an explicit CLI surface, not only environment variables."** Added
   `--host`/`--port`/`--user`/`--password`/`--password-env`/`--database` as real flags (env vars
   remain the defaults, for convenience), following mariachi's own `--password-env`-preferred
   pattern (never `--password` as the primary path, to avoid leaking secrets into shell
   history/process listings).
4. **"mfinspect should support both an exact-instance mode and a search mode"** — a script/`.mf`
   name can never identify a single instance. First proposal included a broader `list` filter set
   (`--root-workflow-id`, `--parent-workflow-id`, `--operation-id`, `--child-workflow-id`) for
   "operators starting from partial context" (an order id, a log line with an operation_id but no
   workflow_id, a blocked child under a known checkout, etc.).
5. **Final correction, narrowing (3) down to the actual first slice:** subcommand-based
   (`inspect`/`list`), global connection args before the action, `list` requires `--script` +
   `--since` + `--until` (bounded scan only) with `--plan-version`/`--state` optional, `inspect`
   drops any time range entirely (full unfiltered dump for a known instance). The broader id-based
   `list` filters from (4) were explicitly discussed and are a deliberate follow-up, not dropped for
   lack of value (see below) — this keeps the first slice small without losing that design intent.

### Verification

Ran against live leftover data in the standing `microflows` DB (from earlier
`call_integration_test.py` runs), through the **built zipapp itself** (not the editable install),
with `PYTHONPATH`/`VIRTUAL_ENV` unset:
- `inspect` on a real `completes`-scenario parent+child tree: correct workflow/plan/args/operations/
  calls/checkpoints/full-events at both levels, child correctly nested under `children`.
- `inspect --max-depth` semantics: confirmed `--max-depth N` expands `call_depth <= N` (matching the
  schema's own numbering), not "N nodes total" (an earlier off-by-one — `depth+1 >= max_depth`
  truncated the FIRST child even at `max_depth=1` — was caught and fixed to `depth+1 > max_depth`
  before the subcommand restructuring).
- Nonexistent `workflow_id` -> clean `{"error": "not_found"}`, no crash.
- Malformed hex `workflow_id` -> `mfinspect error: ...`, exit code 1, no traceback.
- `list --script child --since ... --until ...` -> a bare JSON array; `--state completed` narrows
  it correctly (`state == 4` for every result).
- `list` missing a required flag (e.g. only `--script`, no `--since`/`--until`) -> argparse's own
  "the following arguments are required" error, exit code 2 — enforced structurally, not just
  documented.
- Bad `--password-env` (unset env var) -> clean `mfinspect error: environment variable ... is not
  set`, exit code 1. `--password` direct override works as an explicit escape hatch.
- Global-args-before-action ordering (`--indent 0 inspect ...`, not `inspect ... --indent 0`) behaves
  exactly as argparse subparsers require — confirmed this is the correct/expected shape, matching
  `mariachi`'s own convention, not a bug.

No functional (non-packaging) automated test/fixture was written for the DB-query logic itself —
verification was manual, against live data, as above. The 14 `test_zipapp.py` tests cover the
*packaging* contract exhaustively; they do not exercise `inspect`/`list` query correctness. Worth
revisiting once the tool sees more use: a `db-tests/`-style regression could assert the JSON shape
against a seeded fixture tree, if it starts getting used in `just test` or a review cares.

## Open follow-ups (not this slice)

- Human/tree-formatted output (`--tree` or similar) — deferred per the agreed first-slice scope.
- Broader `list` filters, discussed and deliberately deferred (see review round 4 above), not
  dropped for lack of value: `--root-workflow-id`, `--parent-workflow-id`, `--operation-id`
  (resolve via `tb_mf_operation.operation_id`, unique — "I have an operation_id from a service log
  line, which workflow owns it?"), `--child-workflow-id` (resolve via `tb_mf_call.child_workflow_id`,
  unique — "which parent called this child?"), and later still: `--event-kind`, `--terminal-reason`,
  `--arg key=value`.
- Log ingestion/correlation — deferred per
  `work/workflow-composition/PROGRESS.md`'s 1c observability/correlation note; this slice's JSON
  shape is designed to make that join possible later, but does not implement it.

## Post-landing review: 2 findings, both fixed

**(1) `justfile`'s `inspect`/`list` pass-through recipes conflicted with the CLI contract.** They
hardcoded the subcommand BEFORE `{{ARGS}}` (`"{{MFINSPECT}}" inspect {{ARGS}}`), so `just inspect
<id> --host ...` would run as `mfinspect inspect <id> --host ...` — rejected by argparse, since
`--host` is a global arg that must precede the action. Replaced both recipes with a single generic
`run *ARGS: "{{MFINSPECT}}" "$@"`, matching the actual CLI shape.

Fixing that pass-through recipe surfaced a SECOND, independent bug: plain `{{ARGS}}`
text-interpolation joins variadic arguments into one space-joined string that the recipe's own
shell then re-splits on whitespace — silently breaking any argument containing a space (e.g.
`--since "2026-07-01 00:00:00"` becomes two args, `2026-07-01` and `00:00:00`). Confirmed via a
minimal repro against `just 1.40.0` before touching the real justfile. Fixed with `set
positional-arguments := true` (top of file) + `"$@"` instead of `{{ARGS}}` in the `run` recipe body
— `just` then populates real, boundary-preserving positional shell parameters instead of
interpolating text. Verified: `just run --password-env MDB_ROOT_PWD --indent 0 list --script child
--since "2026-01-01 00:00:00" --until "2099-01-01 00:00:00"` now returns the correct 5-row array
(previously: `argparse: unrecognized arguments: 00:00:00 00:00:00`). All other recipes
(`setup`/`build`/`test`/`help`/`clean`, none of which take variadic args) re-verified unaffected by
the global `positional-arguments` setting.

**(2) `work/workflow-composition/PROGRESS.md`'s top-level "Slice plan" and "Current Scope" sections
were stale** — still describing `mfinspect` as "queued... after the composition slices" and "active
next step" respectively, after it had actually landed. Fixed both to say LANDED, with "active next
step: slice 1c — compensation." (A third, deeper mention inside a DATED historical entry —
"1b.1 is now fully landed... mfinspect (queued... not pulled forward)" — was deliberately left
alone: it's an accurate snapshot of state at the time that entry was written, immediately superseded
by the later "mfinspect first, then 1c compensation" section, consistent with this doc's diary
format throughout.)

## Status

First slice landed, packaged as a mariachi-style zipapp, verified against live data through the
built artifact, and the `justfile` pass-through + top-level doc staleness findings from review are
fixed. Composition work proceeds to **1c compensation** next.
