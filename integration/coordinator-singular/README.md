# integration / coordinator-singular

Cross-component end-to-end suite: the **Microflows coordinator** driving a
**Singular-backed participant** over a real HTTP boundary.

```
microflows-runner (coordinator) --HTTP--> participant-stub --> Singular
        microflows schema                                    singular schema
```

## Why it lives here, not in microflows/
This suite crosses component boundaries — two binaries, two schemas, an HTTP
transport. Keeping it under `integration/` makes that dependency **explicit**
instead of letting a Microflows-local test reach into `../singular/db`.

Component-owned assets stay with their component:
- Microflows stored-procedure tests → `microflows/` (`just test-sp`).
- Singular tests → `singular/`.
- Microflows schema fixtures → `microflows/db-tests/coordinator/scenarios/coordinator-fixtures/`
  (a Microflows schema asset; this suite *applies* them, doesn't own them).

This directory owns only cross-component **orchestration + assertions**
(`justfile` + `test.py` + `tools/emit_test_plan.py`).

## What `just test` does (destructive, isolated setup)
1. **Compile both apps from current source** (mirroring `drift-web`):
   `tools/emit_test_plan.py` emits a build plan; the shared executor compiles the
   `microflows-runner` against the **Microflows library sources** and the
   `participant-stub` against the **Singular library sources**, resolving only
   *external* packages from `DRIFT_PKG_ROOT` (verified against each app's
   committed trust store). Each app's + library's source closure is derived from
   its manifest `modules` (so new/nested modules are picked up automatically).
   Binaries land in a temp work dir. **No deploy, no signing keys, no
   author-claim mutation** — a clean checkout builds, and a stale package can't
   mask a regression. Signed package deploy stays a release operation, out of the
   test path. (DB-free — runs on the executor's compile pool.)
2. Reset the `singular` schema (clean participant store — the participant keys
   operations on `(operation, operation_id)` only, so a clean store keeps the
   fixed-id fixtures hermetic across runs).
3. Reset + seed the `microflows` schema via the `coordinator-fixtures` Mariachi
   scenario.
4. Run `test.py` (with `STUB_BIN`/`RUNNER_BIN` pointing at the work-dir
   binaries), which launches both processes and asserts the properties below.
   (The authoritative pass count is `test.py`'s own `N/N passed` line — not
   restated here, to keep this prose from going stale.)
   - **Forward path:** normal success, lost-ack recovery, idempotent re-run,
     effectively-once execution, initial pending deferral, non-retryable
     rejection, generic string-join dispatch, durable-request recovery,
     inconsistent-terminal-state handling, pinned-contract defer/recover, and
     terminal rerun with the participant down.
   - **Reversal / compensation (single checkpoint):** a reversing workflow
     unwinds its checkpoint stack by dispatching the bound compensation
     (`release`) through the generic dispatcher — normal unwind to `reversed`,
     terminal idempotency (no re-compensation), lost-ack on the reverse dispatch
     (effectively-once, PUT→GET reconcile), restart recovery from a
     durably-dispatched checkpoint (GET-first reconcile — no re-execution **and**
     no re-PUT, asserted via the participant's put-count), the no-active-checkpoint
     inconsistency defer, definite compensation rejection → `blocked_resolution`
     (reverse) with the classified reason, the durable blocked-entry invariants
     (workflow `blocked_resolution`/reverse, checkpoint `resolution_required`, a
     `compensation_blocked` event — read back from the DB), no redispatch while
     blocked, and the missing-compensation-binding deferral.
   - **Multi-checkpoint stack reversal:** a reversing workflow with two active
     checkpoints unwinds highest-seq → lowest, each compensation via its own
     durable binding/input/invocation-id — full unwind to `reversed` (exec
     exactly twice, order proven from the audit events), terminal idempotency (no
     checkpoint compensated twice), mid-stack restart (one checkpoint pre-reversed
     → head advances → only the remainder compensates), and lost-ack on the lower
     checkpoint (effectively-once across the whole stack, proven via exact PUT and
     request deltas through the GET reconcile).

Steps 2–4 run under **one** acquisition of the shared host-global DB lock
(`flocker --key serial-mariadb-mdb114-a -j 1` — the same key the executor uses
for serial DB jobs), so a destructive reset never overlaps another gate's
DB-backed execution on the one shared MariaDB instance.

## Run it
```
export MDB_ROOT_PWD=...
just test              # this suite (does the full setup)
# or from the repo root:
cd ../.. && just test-integration   # all integration suites
cd ../.. && just test               # full repo aggregate (components + integration)
```

## Prereqs
- MariaDB up at `127.0.0.1:34114`; `MDB_ROOT_PWD` set; Mariachi >= 1.0.0.
- `DRIFT_TOOLCHAIN_ROOT` set (the shared executor + `driftc`); `DRIFT_PKG_ROOT`
  for external deps (defaults to the certified libs). **No signing keys and no
  pre-deployed packages** — apps compile from source.

## Gates
- `just test` — the cross-component E2E above.
- `just perf`, `just stress` — explicit successful no-ops (no scenarios yet),
  so the root aggregate never silently skips a missing gate.
