# mfinspect

A read-only inspector for Microflows composition (1b.1) workflow state: `inspect` dumps one known
`workflow_id`'s full durable state and call tree; `list` searches for candidate workflow instances
by script name, time range, plan version, and state. See `work/mfinspect/README.md` at the repo
root for the "why" and design notes.

Packaged the same way as [`mariachi`](../../../../mariachi) — a small Python DB tool with PyMySQL,
shipped as a self-contained zipapp, not an ambient script depending on whichever venv happens to be
active.

## Setup

### Just run it (no install)

A prebuilt, self-contained executable is committed in this directory as **`./mfinspect`**. It
bundles the package, its PyMySQL dependency, and package metadata into a single Python
[zipapp](https://docs.python.org/3/library/zipapp.html). The only requirement on the target machine
is **Python 3.10+** — no virtualenv, no `pip install`:

```
./mfinspect --version
./mfinspect --host 127.0.0.1 --port 34214 --password-env MDB_ROOT_PWD inspect <workflow_id>
./mfinspect --host 127.0.0.1 --port 34214 --password-env MDB_ROOT_PWD \
    list --script checkout --since "2026-07-02 10:00:00" --until "2026-07-02 10:30:00"
```

### Develop / rebuild

For development (editable install, tests) and to regenerate the executable, use
[`just`](https://github.com/casey/just):

```
just setup           # create .venv and install the editable package + PyMySQL
just build           # (re)generate the standalone ./mfinspect zipapp from the venv
just test            # run the packaging test suite (tests/test_zipapp.py)
just run inspect <id>                              # pass-through: .venv/bin/mfinspect inspect <id>
just run --host ... list --script ... --since ... --until ...  # global args before the action
just help
just clean            # remove .venv
```

**Any time source changes, run `just setup && just build` and commit the regenerated
`./mfinspect`** — `tests/test_zipapp.py`'s `CommittedArtifactTests` fails the suite if the committed
artifact drifts from a fresh build (builds are byte-for-byte reproducible, so this is a hash
comparison, not a heuristic).

## CLI

Global DB/output args (before the action):
`--host` `--port` `--user` `--password` `--password-env` `--database` `--indent`

- `inspect <workflow_id> [--max-depth N]` — exact-instance mode. Full recursive JSON tree dump,
  unfiltered (no time range — a known workflow_id gets its entire durable history).
- `list --script NAME --since TS --until TS [--plan-version V] [--state S]` — search/discovery
  mode. `--script`/`--since`/`--until` are all required, deliberately, to rule out an accidental
  full-table scan. Output is a bare JSON array of summaries.

Run `./mfinspect --help`, `./mfinspect inspect --help`, or `./mfinspect list --help` for the full
flag reference.
