#!/usr/bin/env python3
"""Runner-level integration test for composition (1b.1): actual runtime dispatch of
workflow-to-workflow calls, driven through the built `mfrunner` CLI binary against a live DB.

Unlike run_manifest_fixtures.py (build-time-only, DB-connection-free) and sp_call_test.py (SP-layer
only, never touches the runner), this exercises the ACTUAL `_run_forward`/`NeedCall` dispatch and
`_run_reversal`'s `call_kind` branch end to end — those live in non-`pub` functions in runner.drift,
reachable ONLY through the compiled binary's own entry point (main -> _run_manifest/_run_core ->
_run_planned -> _run_forward).

CARRIER PATTERN (approved design, see work/workflow-composition/1b1-integration-test-design.md): a
single mfrunner invocation drives ONE workflow instance to its next durable boundary and exits. The
parent and the child it calls are independently-tracked workflow instances, so testing e.g. "child
completes, parent consumes result" requires three separate subprocess invocations (submit parent ->
drive child -> resume parent). The child's id is learned from the PARENT's own `Pending` JSON output
(`child_workflow_id`, surfaced by this same 1b.1 pass) -- never from a DB query. DB reads below are
used ONLY for out-of-band assertions (e.g. proving replay creates no second child row), never to
drive the subprocess sequence.

STUB PARTICIPANT: every workflow must execute at least one operation (a fail-only or empty graph is
rejected at build time, matching the plan-length-0 gate) -- a bare `call` counts as one executable
step (proven by the gate6_call_only_executable_step fixture), but a CHILD graph with no call of its
own needs a real participant op. A minimal in-process HTTP stub (always 200 `{"result":{}}`) stands
in for `uflowsd_participant_contract.md`'s PUT contract, started once for the whole run.

Requires: the microflows schema loaded (just db-load-schema) + the mfrunner binary built
(cd runner && just build) + MDB_ROOT_PWD. Run via the mariachi venv python (pymysql) or plain python3
(pymysql only needed for the assertion-only DB reads).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql

HOST = os.environ.get("DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("DB_PORT", "34214"))
USER = os.environ.get("DB_USER", "root")
PWD = os.environ.get("MDB_ROOT_PWD", "rootpw")
RUNNER_BIN = os.environ.get("MF_RUNNER_BIN", "")

failures = []
passed = 0


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}: {detail}")


def db():
    return pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD, database="microflows", autocommit=True)


def new_wf_id():
    return uuid.uuid4().hex[:32]


# ===================================================================
# Stub participant: always succeeds with an empty result, per uflowsd_participant_contract.md §4.1
# (PUT 200 -> {"result": {...}}, result mandatory and an object).
# ===================================================================
class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _respond_ok(self):
        body = b'{"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        self._respond_ok()

    def do_GET(self):
        self._respond_ok()


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


def run_cli(manifest_path, workflow_hex, script=None, arguments=None, timeout=30):
    cmd = [RUNNER_BIN, "--manifest", manifest_path, "--workflow-id", workflow_hex]
    if script is not None:
        cmd += ["--script", script, "--arguments", json.dumps(arguments if arguments is not None else {})]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = None
    if p.stdout.strip():
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            pass
    return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "json": out}


# _defer_pending schedules the parent's next_attempt_at PENDING_RETRY_SECONDS (1s) out -- a resume
# attempted before that elapses is correctly refused ("not_yet_due_or_blocked"), not a bug. Wait past
# it before resuming.
def wait_for_due():
    time.sleep(1.3)


def write_manifest(dirpath, scripts_src, stub_url, operations):
    """scripts_src: list of (name, version, mf_source, returns_json). Writes manifest.json + .mf files
    pointed at the REAL live DB (unlike run_manifest_fixtures.py's db.invalid fixtures -- this suite
    needs actual dispatch) and the in-process stub participant."""
    scripts = []
    for name, version, src, returns in scripts_src:
        with open(os.path.join(dirpath, f"{name}.mf"), "w") as f:
            f.write(src)
        scripts.append({"name": name, "version": version, "path": f"{name}.mf", "returns": returns})
    manifest = {
        "deployment": {
            "worker_id": "call-it",
            "db": {"backend": "mariadb", "host": HOST, "port": PORT, "user": USER, "password": PWD, "database": "microflows"},
            "participants": [
                {"id": "stub", "transport": {"kind": "http", "endpoints": [stub_url], "selection": "ordered_failover"}, "auth_profile": None},
            ],
            "operations": operations,
        },
        "scripts": scripts,
    }
    manifest_path = os.path.join(dirpath, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f)
    return manifest_path


UNIT_RETURNS = {}
INT_OBJ_RETURNS = {"type": {"type": "object", "fields": [{"name": "y", "type": {"type": "int"}}]}}

NOOP_OP = {"name": "noop", "participant": "stub", "schema_version": 1}
NOOP_WITH_COMP_OP = {"name": "noop", "participant": "stub", "schema_version": 1,
                     "compensation": {"operation": "noop_undo", "schema_version": 1}}
NOOP_UNDO_OP = {"name": "noop_undo", "participant": "stub", "schema_version": 1}

# Every graph must execute at least one operation (a call OR a participant op) -- a bare `call` alone
# satisfies this for a PARENT (proven by the gate6_call_only_executable_step fixture), but a CHILD with
# no call of its own needs a real participant op first.
CHILD_OK_SRC = "args { }\nop noop { input: {} }\nsteps {\n  noop {}\n}\n"
CHILD_VALUE_SRC = "args { x: int }\nop noop { input: {} }\nsteps {\n  noop {}\n  return { y: arg x }\n}\n"
# "fail-only" is ALSO rejected at build time -- one settled op precedes the authored fail, so reversal
# has exactly one compensable checkpoint to unwind (needs a compensation binding, unlike a call
# checkpoint's own no-op path).
CHILD_FAIL_SRC = "args { }\nop noop { input: {} }\nsteps {\n  noop {}\n  fail \"child_reason\"\n}\n"

PARENT_CALL_ONLY_SRC = "args { }\nsteps {\n  call child@1.0.0 { }\n}\n"
PARENT_CALL_THEN_FAIL_SRC = "args { }\nsteps {\n  call child@1.0.0 { }\n  fail \"boom\"\n}\n"
PARENT_VALUE_SRC = "args { x: int }\nsteps {\n  let r = call child@1.0.0 { x: arg x }\n  return { y: result r.y }\n}\n"


def child_row_count(conn, parent_hex, seq=1):
    with conn.cursor() as c:
        c.execute(
            "SELECT COUNT(*) FROM tb_mf_call WHERE workflow_id = UNHEX(%s) AND operation_seq = %s",
            (parent_hex, seq),
        )
        return c.fetchone()[0]


def workflow_state(conn, wf_hex):
    with conn.cursor() as c:
        c.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id = UNHEX(%s)", (wf_hex,))
        row = c.fetchone()
        return row[0] if row else None


# ===================================================================
# Scenario 1: child completes, parent consumes result.
# ===================================================================
def scenario_child_completes(stub_url):
    with tempfile.TemporaryDirectory() as d:
        mpath = write_manifest(d, [
            ("child", "1.0.0", CHILD_VALUE_SRC, INT_OBJ_RETURNS),
            ("parent", "1.0.0", PARENT_VALUE_SRC, INT_OBJ_RETURNS),
        ], stub_url, [NOOP_OP])
        parent_id = new_wf_id()
        r1 = run_cli(mpath, parent_id, script="parent", arguments={"x": 7})
        pending = r1["json"] is not None and r1["json"].get("workflow") == "pending"
        check("completes: submit parent -> pending", pending, r1)
        if not pending:
            return
        child_id = r1["json"].get("child_workflow_id", "")
        check("completes: pending carries child_workflow_id", bool(child_id), r1["json"])
        if not child_id:
            return

        r2 = run_cli(mpath, child_id)
        check("completes: drive child -> completed", r2["json"] is not None and r2["json"].get("workflow") == "completed", r2)

        wait_for_due()
        r3 = run_cli(mpath, parent_id)
        ok3 = r3["json"] is not None and r3["json"].get("workflow") == "completed"
        check("completes: resume parent -> completed", ok3, r3)
        if ok3:
            check("completes: parent consumes child's result", r3["json"].get("workflow_return") == {"y": 7}, r3["json"])


# ===================================================================
# Scenario 2: child fails, parent begins reversal (no prior checkpoints -> straight to failed).
# ===================================================================
def scenario_child_fails(stub_url):
    with tempfile.TemporaryDirectory() as d:
        mpath = write_manifest(d, [
            ("child", "1.0.0", CHILD_FAIL_SRC, UNIT_RETURNS),
            ("parent", "1.0.0", PARENT_CALL_ONLY_SRC, UNIT_RETURNS),
        ], stub_url, [NOOP_WITH_COMP_OP, NOOP_UNDO_OP])
        parent_id = new_wf_id()
        r1 = run_cli(mpath, parent_id, script="parent", arguments={})
        pending = r1["json"] is not None and r1["json"].get("workflow") == "pending"
        check("fails: submit parent -> pending", pending, r1)
        if not pending:
            return
        child_id = r1["json"].get("child_workflow_id", "")
        check("fails: pending carries child_workflow_id", bool(child_id), r1["json"])
        if not child_id:
            return

        # Drive the child through: its own noop settles, then its authored fail begins ITS OWN
        # reversal (one compensable checkpoint -- noop/noop_undo) down to its own terminal failed.
        r2a = run_cli(mpath, child_id)
        r2 = r2a
        if r2["json"] is not None and r2["json"].get("workflow") == "pending":
            r2 = run_cli(mpath, child_id)
        check("fails: drive child -> failed", r2["json"] is not None and r2["json"].get("workflow") == "failed", r2)

        wait_for_due()
        r3 = run_cli(mpath, parent_id)
        ok3 = r3["json"] is not None and r3["json"].get("workflow") == "failed"
        check("fails: resume parent -> failed (call rejection)", ok3, r3)
        if ok3:
            check("fails: parent's first op has nothing to compensate -> compensated=false",
                  r3["json"].get("compensated") is False, r3["json"])


# ===================================================================
# Scenario 3: child blocked/non-terminal -> parent stays PENDING, never blocked (no block cascade).
# ===================================================================
def scenario_child_non_terminal(stub_url):
    with tempfile.TemporaryDirectory() as d:
        mpath = write_manifest(d, [
            ("child", "1.0.0", CHILD_OK_SRC, UNIT_RETURNS),
            ("parent", "1.0.0", PARENT_CALL_ONLY_SRC, UNIT_RETURNS),
        ], stub_url, [NOOP_OP])
        parent_id = new_wf_id()
        # Submit only -- deliberately never drive the child. A brand-fresh child sits at state=forward,
        # non-terminal, so the parent's very first response already exercises "no block cascade."
        r1 = run_cli(mpath, parent_id, script="parent", arguments={})
        check("non_terminal: submit parent -> pending, NOT blocked",
              r1["json"] is not None and r1["json"].get("workflow") == "pending", r1)


# ===================================================================
# Scenario 4: replay/recovery does not create a second child.
# ===================================================================
def scenario_replay_no_second_child(stub_url):
    with tempfile.TemporaryDirectory() as d:
        mpath = write_manifest(d, [
            ("child", "1.0.0", CHILD_OK_SRC, UNIT_RETURNS),
            ("parent", "1.0.0", PARENT_CALL_ONLY_SRC, UNIT_RETURNS),
        ], stub_url, [NOOP_OP])
        parent_id = new_wf_id()
        r1 = run_cli(mpath, parent_id, script="parent", arguments={})
        pending1 = r1["json"] is not None and r1["json"].get("workflow") == "pending"
        check("replay: fresh submit -> pending", pending1, r1)
        if not pending1:
            return
        child_id_1 = r1["json"].get("child_workflow_id", "")

        # Resume (no --script/--arguments) BEFORE ever driving the child: the parent re-enters the
        # same NeedCall arm, re-calls call_submit (idempotent replay), re-inspects (still non-terminal).
        wait_for_due()
        r2 = run_cli(mpath, parent_id)
        pending2 = r2["json"] is not None and r2["json"].get("workflow") == "pending"
        check("replay: resume -> still pending", pending2, r2)
        if not pending2:
            return
        child_id_2 = r2["json"].get("child_workflow_id", "")

        check("replay: same child_workflow_id across both responses",
              bool(child_id_1) and child_id_1 == child_id_2, (child_id_1, child_id_2))

        conn = db()
        try:
            check("replay: exactly one row in tb_mf_call",
                  child_row_count(conn, parent_id) == 1, child_row_count(conn, parent_id))
        finally:
            conn.close()


# ===================================================================
# Scenario 5: call checkpoint reverses as a no-op.
# ===================================================================
def scenario_call_checkpoint_noop_reversal(stub_url):
    with tempfile.TemporaryDirectory() as d:
        mpath = write_manifest(d, [
            ("child", "1.0.0", CHILD_OK_SRC, UNIT_RETURNS),
            ("parent", "1.0.0", PARENT_CALL_THEN_FAIL_SRC, UNIT_RETURNS),
        ], stub_url, [NOOP_OP])
        parent_id = new_wf_id()
        r1 = run_cli(mpath, parent_id, script="parent", arguments={})
        pending = r1["json"] is not None and r1["json"].get("workflow") == "pending"
        check("checkpoint_noop: submit parent -> pending", pending, r1)
        if not pending:
            return
        child_id = r1["json"].get("child_workflow_id", "")
        check("checkpoint_noop: pending carries child_workflow_id", bool(child_id), r1["json"])
        if not child_id:
            return

        r2 = run_cli(mpath, child_id)
        check("checkpoint_noop: drive child -> completed", r2["json"] is not None and r2["json"].get("workflow") == "completed", r2)

        # The call (seq=1) settles as a non-final checkpoint, then seq=2's authored `fail "boom"`
        # begins reversal; reverse_head's FIRST checkpoint IS the call -> checkpoint_reverse_noop ->
        # terminal, all within this single resume.
        wait_for_due()
        r3 = run_cli(mpath, parent_id)
        ok3 = r3["json"] is not None and r3["json"].get("workflow") == "failed"
        check("checkpoint_noop: resume parent -> failed (reversal complete)", ok3, r3)
        if ok3:
            check("checkpoint_noop: compensated=true (the no-op reversal ran)",
                  r3["json"].get("compensated") is True, r3["json"])

        conn = db()
        try:
            check("checkpoint_noop: parent workflow reaches REVERSED state (5)",
                  workflow_state(conn, parent_id) == 5, workflow_state(conn, parent_id))
        finally:
            conn.close()


def main():
    if not RUNNER_BIN or not os.path.exists(RUNNER_BIN):
        print(f"error: MF_RUNNER_BIN not found: {RUNNER_BIN!r} (build it: cd runner && just build)", file=sys.stderr)
        return 2
    server, stub_url = start_stub()
    try:
        scenario_child_completes(stub_url)
        scenario_child_fails(stub_url)
        scenario_child_non_terminal(stub_url)
        scenario_replay_no_second_child(stub_url)
        scenario_call_checkpoint_noop_reversal(stub_url)
    finally:
        server.shutdown()
    print(f"call_integration regression: {passed}/{passed + len(failures)} passed (expected {passed + len(failures)})")
    if failures:
        print(f"FAILED: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
