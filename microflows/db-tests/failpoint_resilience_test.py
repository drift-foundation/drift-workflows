#!/usr/bin/env python3
"""Pinned resilience tests: AmbiguousWrite (lost COMMIT ack) on each of the 5 dispatch-path SP calls
identified in work/workflows-failpoint-resilience/DESIGN.md, run through mariadb-failpoint-proxy's
one-shot ack-loss failpoint against a REAL MariaDB. Proves the runner.drift/host.drift fix (the
"_checked"/"_core" errors-as-values split + _defer_on_backend_unavailable) actually holds under an
induced ambiguous commit, not just in theory.

For each target this asserts, per the resilience-effort's acceptance checklist:
  1. no `runner: fatal` / non-JSON crash (returncode != 1 with empty json)
  2. a clean, structured retriable JSON outcome ({"workflow": "deferred", ...} -- Outcome::Deferred,
     NOT the unrelated Outcome::Pending an ordinary in-flight call-await also renders as "pending")
  3. the lease is released/deferred PROMPTLY (next_attempt_at ~= DISPATCH_DEFER_SECONDS out, not the
     full LEASE_SECONDS) rather than held for the full lease TTL
  4. a retry/resume after the short defer converges correctly: same child_workflow_id where
     applicable, no duplicate rows, workflow reaches its expected terminal state
  5. the failpoint genuinely armed AND fired (assert_all_fired) -- without this a test that forgot
     to arm correctly (e.g. a missing required `label` field) can vacuously pass on the unfaulted path

Ordinals below are ground truth, determined via mysql.general_log tracing (see
work/workflows-failpoint-resilience/PROGRESS.md) -- never guessed.

Requires: schema loaded (just db-load-schema && just db-load-test-fixtures), mfrunner built
(cd runner && just build), mariadb-failpoint-proxy built (drift-mariadb-client: just build-app
mariadb-failpoint-proxy) and running with --backend-host/port pointed at the real dev DB. Run via
the mariachi venv python (pymysql): /home/sl/src/mariachi/.venv/bin/python3 failpoint_resilience_test.py
"""
import datetime
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql

sys.path.insert(0, os.path.dirname(__file__))
import call_integration_test as t

# Real DB (never proxied -- out-of-band assertions must not be affected by an armed failpoint).
REAL_HOST = os.environ.get("REAL_DB_HOST", "127.0.0.1")
REAL_PORT = int(os.environ.get("REAL_DB_PORT", "34214"))
# Proxy's data listener (mfrunner's manifest points here).
PROXY_HOST = os.environ.get("PROXY_DATA_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_DATA_PORT", "43306"))
PROXY_CONTROL_HOST = os.environ.get("PROXY_CONTROL_HOST", "127.0.0.1")
PROXY_CONTROL_PORT = int(os.environ.get("PROXY_CONTROL_PORT", "43307"))

USER = "root"
PWD = "rootpw"
RUNNER_BIN = os.environ.get("MF_RUNNER_BIN", "/home/sl/src/drift-workflows/microflows/runner/build/dist/bin/mfrunner")

DISPATCH_DEFER_SECONDS = 5
LEASE_SECONDS = 30

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


class ProxyControlClient:
    """~20-line raw TCP JSON-Lines client for mariadb-failpoint-proxy's control protocol."""

    def __init__(self, host=PROXY_CONTROL_HOST, port=PROXY_CONTROL_PORT):
        self.host = host
        self.port = port

    def _req(self, obj):
        s = socket.create_connection((self.host, self.port), timeout=5)
        try:
            s.sendall((json.dumps(obj) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode())
        finally:
            s.close()

    def health(self):
        return self._req({"op": "health"})

    def clear(self):
        return self._req({"op": "clear"})

    def arm(self, nth=1, action="drop_server_response_after_forward", hold_ms=None, label=None):
        obj = {"op": "arm", "label": label or f"failpoint-resilience-{nth}-{action}", "match": {"nth": nth}, "action": action}
        if hold_ms is not None:
            obj["hold_ms"] = hold_ms
        return self._req(obj)

    def assert_all_fired(self):
        return self._req({"op": "assert_all_fired"})

    def status(self):
        return self._req({"op": "status"})


def real_db():
    return pymysql.connect(host=REAL_HOST, port=REAL_PORT, user=USER, password=PWD, database="microflows", autocommit=True)


def write_proxied_manifest(dirpath, scripts_src, stub_url, operations):
    """Same shape as call_integration_test.write_manifest, but db.host/port point at the PROXY's
    data listener -- the one knob needed to route this mfrunner invocation's traffic through it."""
    scripts = []
    for name, version, src, returns in scripts_src:
        with open(os.path.join(dirpath, f"{name}.mf"), "w") as f:
            f.write(src)
        scripts.append({"name": name, "version": version, "path": f"{name}.mf", "returns": returns})
    manifest = {
        "deployment": {
            "worker_id": "failpoint-it",
            "db": {"backend": "mariadb", "host": PROXY_HOST, "port": PROXY_PORT, "user": USER, "password": PWD, "database": "microflows"},
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


def lease_row(conn, wf_hex):
    with conn.cursor() as c:
        c.execute(
            "SELECT lease_owner, lease_expires_at, next_attempt_at, state, NOW(6) FROM tb_mf_workflow WHERE workflow_id = UNHEX(%s)",
            (wf_hex,),
        )
        row = c.fetchone()
        if not row:
            return None
        return {"lease_owner": row[0], "lease_expires_at": row[1], "next_attempt_at": row[2], "state": row[3], "db_now": row[4]}


def assert_prompt_lease_release(name, conn, wf_hex):
    """Lease must be released/short-deferred (~DISPATCH_DEFER_SECONDS out), not held for the full
    LEASE_SECONDS TTL. A generous upper bound (LEASE_SECONDS/2) distinguishes 'fixed, short defer'
    from 'old bug, held for the full TTL' without being a flaky exact-timing assertion."""
    row = lease_row(conn, wf_hex)
    check(f"{name}: workflow row exists after crash-that-no-longer-crashes", row is not None, row)
    if row is None:
        return
    check(f"{name}: lease_owner cleared (not held)", row["lease_owner"] is None, row)
    if row["next_attempt_at"] is not None and row["db_now"] is not None:
        delta = (row["next_attempt_at"] - row["db_now"]).total_seconds()
        check(f"{name}: next_attempt_at is a SHORT defer (< {LEASE_SECONDS/2}s out), not the full lease TTL",
              0 <= delta < (LEASE_SECONDS / 2), f"delta={delta}s next_attempt_at={row['next_attempt_at']} db_now={row['db_now']}")


def assert_fault_fired(name, proxy):
    """The single most important check in this file: a test that forgot to actually arm the
    failpoint (or where the arm silently no-op'd) must FAIL here, not vacuously pass because the
    unfaulted code path also happens to look fine. See docs' own assert_all_fired rationale."""
    res = proxy.assert_all_fired()
    check(f"{name}: failpoint genuinely armed AND fired (not a vacuous pass)", res.get("ok") is True, res)


def assert_clean_retriable(name, result):
    """Expected outcome when the fix works: Outcome::Deferred (JSON key "deferred", exit code 9 via
    _defer_dispatch) -- NOT "runner: fatal", and NOT to be confused with the unrelated Outcome::Pending
    ("pending") a normal in-flight call-await also renders with."""
    check(f"{name}: no runner fatal (returncode != 1 with no json)",
          not (result["returncode"] == 1 and result["json"] is None), result)
    check(f"{name}: stderr has no 'runner: fatal'", "runner: fatal" not in result["stderr"], result["stderr"])
    check(f"{name}: clean structured JSON outcome present", result["json"] is not None, result)
    if result["json"] is not None:
        check(f"{name}: JSON reports Outcome::Deferred (the fix's retriable path), not a crash",
              result["json"].get("workflow") == "deferred", result["json"])


# ===================================================================
# Target 1: sp_mf_call_submit (composition parent-op + child-workflow-creation boundary).
# Commit ordinal 9 of 12 in a fresh "submit parent" drive -- ground truth via general_log,
# cross-checked against the proxy's own commit_observed log (see PROGRESS.md Step 3a).
# ===================================================================
def target_call_submit():
    name = "call_submit"
    proxy = ProxyControlClient()
    proxy.clear()
    _, stub_url = start_stub()
    with tempfile.TemporaryDirectory() as d:
        mpath = write_proxied_manifest(d, [
            ("child", "1.0.0", t.CHILD_VALUE_SRC, t.INT_OBJ_RETURNS),
            ("parent", "1.0.0", t.PARENT_VALUE_SRC, t.INT_OBJ_RETURNS),
        ], stub_url, [t.NOOP_OP])
        parent_id = t.new_wf_id()

        proxy.arm(nth=9, action="drop_server_response_after_forward")
        r1 = run_cli(mpath, parent_id, script="parent", arguments={"x": 7})
        assert_clean_retriable(name, r1)
        assert_fault_fired(name, proxy)

        real_conn = real_db()
        try:
            assert_prompt_lease_release(name, real_conn, parent_id)
            child_row_count = _child_row_count(real_conn, parent_id)
            check(f"{name}: exactly one call row after the ambiguous-write attempt", child_row_count == 1, child_row_count)
        finally:
            real_conn.close()

        time.sleep(DISPATCH_DEFER_SECONDS + 1)
        r2 = run_cli(mpath, parent_id)
        child_id_2 = r2["json"].get("child_workflow_id", "") if r2["json"] else ""
        check(f"{name}: retry resumes cleanly (pending, same child_workflow_id)",
              r2["json"] is not None and r2["json"].get("workflow") == "pending" and bool(child_id_2), r2)

        real_conn = real_db()
        try:
            check(f"{name}: still exactly one call row + one child workflow row after retry",
                  _child_row_count(real_conn, parent_id) == 1, _child_row_count(real_conn, parent_id))
        finally:
            real_conn.close()

        r3 = run_cli(mpath, child_id_2) if child_id_2 else {"json": None}
        check(f"{name}: child drives to completion", r3["json"] is not None and r3["json"].get("workflow") == "completed", r3)

        time.sleep(1.3)
        r4 = run_cli(mpath, parent_id)
        check(f"{name}: parent converges to completed after retry", r4["json"] is not None and r4["json"].get("workflow") == "completed", r4)


def _child_row_count(conn, parent_hex, seq=1):
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM tb_mf_call WHERE workflow_id = UNHEX(%s) AND operation_seq = %s", (parent_hex, seq))
        return c.fetchone()[0]


def _op_row_count(conn, wf_hex, seq=1):
    with conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id = UNHEX(%s) AND operation_seq = %s", (wf_hex, seq))
        return c.fetchone()[0]


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        body = b'{"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.do_PUT()


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    return server, f"http://127.0.0.1:{port}"


# ===================================================================
# Target 2: sp_mf_operation_request (participant DISPATCH -- app-team's #1 ask). Commit ordinal 10
# of 14 in a fresh standalone (no composition) single-participant-op workflow.
# ===================================================================
def target_operation_request():
    name = "operation_request"
    proxy = ProxyControlClient()
    proxy.clear()
    _, stub_url = start_stub()
    with tempfile.TemporaryDirectory() as d:
        mpath = write_proxied_manifest(d, [("solo", "1.0.0", t.CHILD_OK_SRC, t.UNIT_RETURNS)], stub_url, [t.NOOP_OP])
        wf_id = t.new_wf_id()

        proxy.arm(nth=10, action="drop_server_response_after_forward")
        r1 = run_cli(mpath, wf_id, script="solo", arguments={})
        assert_clean_retriable(name, r1)
        assert_fault_fired(name, proxy)

        real_conn = real_db()
        try:
            assert_prompt_lease_release(name, real_conn, wf_id)
        finally:
            real_conn.close()

        time.sleep(DISPATCH_DEFER_SECONDS + 1)
        r2 = run_cli(mpath, wf_id)
        check(f"{name}: retry converges to completed (idempotent replay, no duplicate dispatch)",
              r2["json"] is not None and r2["json"].get("workflow") == "completed", r2)

        real_conn = real_db()
        try:
            check(f"{name}: exactly one operation row (no duplicate participant dispatch)",
                  _op_row_count(real_conn, wf_id) == 1, _op_row_count(real_conn, wf_id))
        finally:
            real_conn.close()


# ===================================================================
# Target 3: sp_mf_operation_settle (recording the participant's result -- app-team's #2 ask).
# Commit ordinal 12 of 14 in the SAME standalone scenario.
# ===================================================================
def assert_clean_retriable_or_terminal_race(name, result, proxy):
    """For a FINAL settle (the underlying write ALSO completes/terminates the workflow): when its
    COMMIT ack is lost, the write is still durable -- the workflow reaches its terminal state BEFORE
    _defer_on_backend_unavailable's own follow-up _defer call runs, so that follow-up legitimately
    sees a stale fencing_token and gets FenceLost -> Outcome::DeferFailed(reason="release_fence_lost").
    This is CORRECT, not a bug: the workflow is already safely terminal by then -- "prompt release"
    happened via the settle's own commit, not via a defer. Accepts Outcome::Deferred too, in case a
    site's own defer legitimately still succeeds (e.g. if it were NOT the final settle)."""
    check(f"{name}: no runner fatal (returncode != 1 with no json)",
          not (result["returncode"] == 1 and result["json"] is None), result)
    check(f"{name}: stderr has no 'runner: fatal'", "runner: fatal" not in result["stderr"], result["stderr"])
    check(f"{name}: clean structured JSON outcome present", result["json"] is not None, result)
    if result["json"] is not None:
        outcome = result["json"].get("workflow")
        check(f"{name}: JSON reports Outcome::Deferred OR DeferFailed(release_fence_lost) -- "
              f"the latter is correct here since this settle's own commit already completed "
              f"the workflow before the follow-up defer call ran", outcome in ("deferred", "defer_failed"), result["json"])
        if outcome == "defer_failed":
            check(f"{name}: DeferFailed reason is the expected release_fence_lost (not some other failure)",
                  result["json"].get("reason") == "release_fence_lost", result["json"])
    assert_fault_fired(name, proxy)


def target_operation_settle():
    name = "operation_settle"
    proxy = ProxyControlClient()
    proxy.clear()
    _, stub_url = start_stub()
    with tempfile.TemporaryDirectory() as d:
        mpath = write_proxied_manifest(d, [("solo", "1.0.0", t.CHILD_OK_SRC, t.UNIT_RETURNS)], stub_url, [t.NOOP_OP])
        wf_id = t.new_wf_id()

        proxy.arm(nth=12, action="drop_server_response_after_forward")
        r1 = run_cli(mpath, wf_id, script="solo", arguments={})
        assert_clean_retriable_or_terminal_race(name, r1, proxy)

        real_conn = real_db()
        try:
            row = lease_row(real_conn, wf_id)
            check(f"{name}: workflow row exists after crash-that-no-longer-crashes", row is not None, row)
            if row is not None:
                check(f"{name}: lease_owner cleared (released, one way or another)", row["lease_owner"] is None, row)
                check(f"{name}: workflow already reached state=completed(4) -- the settle's own commit "
                      f"WAS the prompt release, durable despite the lost ack", row["state"] == 4, row)
        finally:
            real_conn.close()

        r2 = run_cli(mpath, wf_id)
        check(f"{name}: immediate resume converges to already_terminal/completed (idempotent, no duplicate settle)",
              r2["json"] is not None and r2["json"].get("workflow") in ("completed", "already_terminal"), r2)

        real_conn = real_db()
        try:
            check(f"{name}: exactly one operation row (no duplicate settle)",
                  _op_row_count(real_conn, wf_id) == 1, _op_row_count(real_conn, wf_id))
        finally:
            real_conn.close()


# ===================================================================
# Target 4: sp_mf_checkpoint_reverse_child_reopen. Commit ordinal 16 of 19, in the THIRD mfrunner
# invocation of the reverse_child scenario (the resume that discovers the authored fail and reopens
# the completed child into reversal).
# ===================================================================
def target_checkpoint_reverse_child_reopen():
    name = "checkpoint_reverse_child_reopen"
    proxy = ProxyControlClient()
    proxy.clear()
    _, stub_url = start_stub()
    with tempfile.TemporaryDirectory() as d:
        mpath = write_proxied_manifest(d, [
            ("child", "1.0.0", t.CHILD_OK_SRC, t.UNIT_RETURNS),
            ("parent", "1.0.0", t.PARENT_CALL_THEN_FAIL_SRC, t.UNIT_RETURNS),
        ], stub_url, [t.NOOP_WITH_COMP_OP, t.NOOP_UNDO_OP])
        parent_id = t.new_wf_id()

        r1 = run_cli(mpath, parent_id, script="parent", arguments={})
        check(f"{name}: setup -- submit parent -> pending", r1["json"] is not None and r1["json"].get("workflow") == "pending", r1)
        child_id = r1["json"]["child_workflow_id"]

        r2 = run_cli(mpath, child_id)
        check(f"{name}: setup -- drive child -> completed", r2["json"] is not None and r2["json"].get("workflow") == "completed", r2)

        time.sleep(1.3)
        proxy.arm(nth=16, action="drop_server_response_after_forward")
        r3 = run_cli(mpath, parent_id)
        assert_clean_retriable(name, r3)
        assert_fault_fired(name, proxy)

        real_conn = real_db()
        try:
            assert_prompt_lease_release(name, real_conn, parent_id)
        finally:
            real_conn.close()

        time.sleep(DISPATCH_DEFER_SECONDS + 1)
        r4 = run_cli(mpath, parent_id)
        check(f"{name}: retry re-reopens (idempotent AlreadyReopened) and defers on child non-terminal",
              r4["json"] is not None and r4["json"].get("workflow") == "pending", r4)

        r5 = run_cli(mpath, child_id)
        check(f"{name}: drive child's own compensation -> failed", r5["json"] is not None and r5["json"].get("workflow") == "failed", r5)

        time.sleep(1.3)
        r6 = run_cli(mpath, parent_id)
        check(f"{name}: parent eventually converges to failed/compensated", r6["json"] is not None and r6["json"].get("workflow") == "failed", r6)
        if r6["json"] is not None:
            check(f"{name}: compensated=true", r6["json"].get("compensated") is True, r6["json"])


# ===================================================================
# Target 5: sp_mf_checkpoint_reverse_child_settle. Commit ordinal 10 of 12, in the FIFTH mfrunner
# invocation (the resume that observes the child's own reversal completed and settles the parent's
# checkpoint).
# ===================================================================
def target_checkpoint_reverse_child_settle():
    name = "checkpoint_reverse_child_settle"
    proxy = ProxyControlClient()
    proxy.clear()
    _, stub_url = start_stub()
    with tempfile.TemporaryDirectory() as d:
        mpath = write_proxied_manifest(d, [
            ("child", "1.0.0", t.CHILD_OK_SRC, t.UNIT_RETURNS),
            ("parent", "1.0.0", t.PARENT_CALL_THEN_FAIL_SRC, t.UNIT_RETURNS),
        ], stub_url, [t.NOOP_WITH_COMP_OP, t.NOOP_UNDO_OP])
        parent_id = t.new_wf_id()

        r1 = run_cli(mpath, parent_id, script="parent", arguments={})
        child_id = r1["json"]["child_workflow_id"]
        run_cli(mpath, child_id)
        time.sleep(1.3)
        r3 = run_cli(mpath, parent_id)
        check(f"{name}: setup -- parent reopens child, defers (non-terminal)",
              r3["json"] is not None and r3["json"].get("workflow") == "pending", r3)
        r4 = run_cli(mpath, child_id)
        check(f"{name}: setup -- drive child's own compensation -> failed", r4["json"] is not None and r4["json"].get("workflow") == "failed", r4)

        # This is the LAST checkpoint before the parent itself reaches its terminal reversed(5) state
        # -- the same "final settle" nuance as operation_settle: the underlying write is durable, but
        # a FenceLost follow-up defer is the CORRECT outcome (not a bug), since the workflow is already
        # terminal by the time _defer_on_backend_unavailable's own follow-up call runs.
        time.sleep(1.3)
        proxy.arm(nth=10, action="drop_server_response_after_forward")
        r5 = run_cli(mpath, parent_id)
        assert_clean_retriable_or_terminal_race(name, r5, proxy)

        real_conn = real_db()
        try:
            row = lease_row(real_conn, parent_id)
            check(f"{name}: workflow row exists after crash-that-no-longer-crashes", row is not None, row)
            if row is not None:
                check(f"{name}: lease_owner cleared (released, one way or another)", row["lease_owner"] is None, row)
                check(f"{name}: parent already reached state=reversed(5) -- the settle's own commit "
                      f"WAS the prompt release, durable despite the lost ack", row["state"] == 5, row)
        finally:
            real_conn.close()

        r6 = run_cli(mpath, parent_id)
        check(f"{name}: immediate resume converges to failed/compensated (idempotent settle, no duplicate)",
              r6["json"] is not None and r6["json"].get("workflow") == "failed", r6)
        if r6["json"] is not None:
            check(f"{name}: compensated=true", r6["json"].get("compensated") is True, r6["json"])


def main():
    targets = {
        "call_submit": target_call_submit,
        "operation_request": target_operation_request,
        "operation_settle": target_operation_settle,
        "checkpoint_reverse_child_reopen": target_checkpoint_reverse_child_reopen,
        "checkpoint_reverse_child_settle": target_checkpoint_reverse_child_settle,
    }
    which = sys.argv[1:] if len(sys.argv) > 1 else list(targets.keys())
    for name in which:
        print(f"\n### {name} ###")
        targets[name]()
    print(f"\n{passed} passed, {len(failures)} failed")
    if failures:
        print("FAILURES:", failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
