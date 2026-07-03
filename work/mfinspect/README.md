# mfinspect

A read-only debugging/inspection tool for Microflows composition (1b.1/1c) workflow state.

## Why

1b.1 lets a parent workflow sit `pending` on a child call, and a blocked descendant deliberately
does not cascade up the call tree (see `work/workflow-composition/DESIGN.md`); 1c extends this with
reverse-child compensation, where a parent's own reversal can likewise sit `pending` on a child's
in-flight compensation. Either way, "what is this workflow actually waiting on?" cannot be answered
from a single row; it requires walking `tb_mf_call.child_workflow_id` down the tree by hand.
`mfinspect` answers that question directly from the coordinator DB.

Decided to build this before 1c compensation (see `work/workflow-composition/PROGRESS.md` §"mfinspect
first, then 1c compensation"), since 1c's own reversal-across-a-tree integration work needed it
immediately.

## What it is

- Location: `microflows/tools/mfinspect/` — a standalone Python package, packaged and built the
  same way as [`mariachi`](../../../mariachi) (`pyproject.toml`, console-script entry point, a
  committed self-contained zipapp executable at `microflows/tools/mfinspect/mfinspect`, built by
  `tools/build_zipapp.py`, verified by `tests/test_zipapp.py`). Not an ambient script depending on
  whichever venv happens to be active — see `microflows/tools/mfinspect/README.md` for build/run
  instructions.
- Two actions, because a script/`.mf` name is not an instance identity (many workflow instances can
  run the same script):
  - **`inspect <workflow_id> [--max-depth N]`** — exact-instance mode. Full recursive JSON tree
    dump for one known `workflow_id`: workflow row, plan pin + args, operations, call sidecar rows,
    checkpoint stack, and the FULL event history (no time filtering — a known instance gets
    everything), with `children` nesting recursively into each call's child workflow up to
    `--max-depth`.
  - **`list --script NAME --since TS --until TS [--plan-version V] [--state S]`** — search/discovery
    mode. `--script`/`--since`/`--until` are all required, deliberately, to rule out an accidental
    full-table scan in production. Output is a bare JSON array of summaries (workflow_id,
    script_name, plan_version, state/state_name/direction/disposition, parent/root ids, created_at,
    latest event timestamp, terminal_reason) — never a tree. Pick a `workflow_id` from the results,
    then `inspect` it.
- DB connection is an explicit CLI surface (`--host`/`--port`/`--user`/`--password`/
  `--password-env`/`--database`), not only environment variables — with env-var defaults for
  convenience (`DB_HOST`/`DB_PORT`/`DB_USER`/`DB_NAME`/`MDB_ROOT_PWD`). `--password-env` (default
  `MDB_ROOT_PWD`) is preferred over `--password` so a password never lands in shell history or a
  process listing; `--password` exists as an explicit override for local dev ergonomics.
- **Read-only, always.** No claim/resume/notify/unblock/timer-mutation path exists in the tool at
  all — every DB access is a `SELECT`.

## What it is not (yet)

- No human/tree-formatted output — JSON only, first slice. A `--tree` renderer is a later slice.
- No log ingestion/correlation — the JSON output preserves every correlation-relevant field
  (`workflow_id`, `operation_seq`, `operation_id`, `operation_name`, checkpoint `seq`,
  `child_workflow_id`, `parent_workflow_id`, event `kind`/timestamp) so a later pass can join
  against service logs, but this slice does not read logs itself.
- `list`'s filter set is deliberately narrow for the first slice (script + bounded time range,
  plus optional plan-version/state). Additional filters were discussed and intentionally deferred,
  not dropped for lack of value — see `Progress.md` "Open follow-ups."

## Usage

```
cd microflows/tools/mfinspect
./mfinspect --version                                   # prebuilt zipapp, no venv needed
./mfinspect --password-env MDB_ROOT_PWD inspect <workflow_id_hex>
./mfinspect --password-env MDB_ROOT_PWD \
    list --script checkout --since "2026-07-02 10:00:00" --until "2026-07-02 10:30:00"
```

See `microflows/tools/mfinspect/README.md` for the full build/develop workflow (`just setup` /
`just build` / `just test`) and `Progress.md` (this folder) for implementation status.
