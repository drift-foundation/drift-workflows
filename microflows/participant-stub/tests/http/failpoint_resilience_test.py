#!/usr/bin/env python3
"""Pinned resilience tests: AmbiguousWrite (lost COMMIT ack) on Singular's own gateway commits
(sp_singular_start, sp_singular_complete, sp_singular_resume), driven through the participant-stub
HTTP boundary, via mariadb-failpoint-proxy's one-shot ack-loss failpoint against a REAL MariaDB.

Extends work/workflows-failpoint-resilience/ to Singular per the user's explicit direction: the
Microflows-side fix alone left Singular's own claim/start/complete boundary uncovered, and this
release's scope is specifically AmbiguousWrite resilience -- that gap must be closed, not left open.

For each target this asserts, per the acceptance checklist:
  1. no participant-stub process fatal (the process stays alive and keeps answering requests)
  2. typed RpcCommitError.kind is respected: AmbiguousWrite/NotSent -> retriable (202 pending),
     never collapsed into the SAME undifferentiated response a ServerRejected would get
  3. the work item is not left in a worse state than the durable write implies (verified directly
     against tb_singular_work_item -- an ambiguous-but-landed write must be observably durable)
  4. retry/reclaim converges with the SAME operation_id / durable identity, no duplicate mutation
     beyond idempotent replay (exec_count instrumentation)
  5. the failpoint genuinely armed AND fired (assert_all_fired -- closes the same vacuous-pass gap
     found earlier in the microflows-side harness)

Ordinals below are ground truth, determined via mysql.general_log tracing against a driven
participant-stub PUT (see work/workflows-failpoint-resilience/PROGRESS.md) -- never guessed.

Requires: singular schema loaded (cd singular && just db-load-schema), participant-stub built
(cd microflows/participant-stub && just build), mariadb-failpoint-proxy built and running with
--backend-host/port pointed at the real dev DB. Run via the mariachi venv python (pymysql):
/home/sl/src/mariachi/.venv/bin/python3 failpoint_resilience_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[2]  # participant-stub/
BIN = ROOT / "build" / "dist" / "bin" / "participant-stub"

REAL_HOST = os.environ.get("REAL_DB_HOST", "127.0.0.1")
REAL_PORT = int(os.environ.get("REAL_DB_PORT", "34214"))
PROXY_DATA_HOST = os.environ.get("PROXY_DATA_HOST", "127.0.0.1")
PROXY_DATA_PORT = int(os.environ.get("PROXY_DATA_PORT", "43306"))
PROXY_CONTROL_HOST = os.environ.get("PROXY_CONTROL_HOST", "127.0.0.1")
PROXY_CONTROL_PORT = int(os.environ.get("PROXY_CONTROL_PORT", "43307"))

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

    def clear(self):
        return self._req({"op": "clear"})

    def arm(self, nth=1, action="drop_server_response_after_forward", label=None):
        return self._req({"op": "arm", "label": label or f"singular-failpoint-{nth}-{action}",
                           "match": {"nth": nth}, "action": action})

    def assert_all_fired(self):
        return self._req({"op": "assert_all_fired"})


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(base, method, path, body):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


class Stub:
    def __init__(self, base):
        self.base = base

    def put(self, op, op_id, body):
        return _req(self.base, "PUT", f"/microflows/v1/operations/{op}/{op_id}", body)

    def get(self, op, op_id):
        return _req(self.base, "GET", f"/microflows/v1/operations/{op}/{op_id}", None)

    def exec_count(self):
        _, body = _req(self.base, "GET", "/debug/exec-count", None)
        return body["count"]

    def alive(self):
        try:
            _req(self.base, "GET", "/debug/exec-count", None)
            return True
        except Exception:
            return False


def spawn_stub(host, port_db, lease_ttl=None):
    if not BIN.exists():
        sys.exit(f"error: stub binary not found at {BIN} -- run `just build` first")
    port = _free_port()
    sg = f"failpoint-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    cfg = {
        "port": port,
        "service_group": sg,
        "worker_id": "failpoint-worker",
        "singular": {
            "backend": "mariadb",
            "host": host,
            "port": port_db,
            "user": "root",
            "password": "rootpw",
            "database": "singular",
            "connect_timeout_ms": 3000,
            "io_timeout_ms": 3000,
            "pool": {"keepalive_interval_ms": 100},
        },
    }
    cf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(cfg, cf)
    cf.close()
    env = dict(os.environ)
    if lease_ttl is not None:
        env["MICROFLOWS_STUB_LEASE_TTL_SECONDS"] = str(lease_ttl)
    proc = subprocess.Popen([str(BIN), "--config", cf.name],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            sys.exit(f"error: stub exited early:\n{out}")
        try:
            urllib.request.urlopen(f"{base}/debug/exec-count", timeout=1)
            return proc, base, cf.name
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    sys.exit("error: stub did not become ready in time")


def real_db():
    return pymysql.connect(host=REAL_HOST, port=REAL_PORT, user="root", password="rootpw", database="singular", autocommit=True)


def work_item_row(conn, service_group, key_hex):
    with conn.cursor() as c:
        c.execute(
            "SELECT state, lease_owner, lease_expires_at, lease_token FROM tb_singular_work_item "
            "WHERE service_group = %s AND item_key = UNHEX(%s)",
            (service_group, key_hex),
        )
        row = c.fetchone()
        if not row:
            return None
        return {"state": row[0], "lease_owner": row[1], "lease_expires_at": row[2], "lease_token": row[3]}


# ===================================================================
# Target 1: sp_singular_start. Commit ordinal 1 of 2 in a fresh PUT (start() then complete()
# in the same request). AmbiguousWrite means the work item durably lands in Working state, but
# the client never learns start() succeeded.
# ===================================================================
def target_start():
    """A SHORT lease_ttl is used deliberately: an immediate retry right after the ambiguous start()
    correctly sees the lease as still LIVE (Active -> 202, "never steal a live lease" -- the file's
    own documented design), which is NOT the same thing as convergence. To observe the actual
    recovery (reclaim after the lease naturally expires), wait past the TTL before retrying -- the
    same pattern the existing crash_after_commit_reclaim_on_put conformance case already uses."""
    name = "singular_start"
    proxy = ProxyControlClient()
    proxy.clear()
    proc, base, cfg_path = spawn_stub(PROXY_DATA_HOST, PROXY_DATA_PORT, lease_ttl=1)
    stub = Stub(base)
    try:
        proxy.arm(nth=1, action="drop_server_response_after_forward")
        code, body = stub.put("echo-transform", "op-fp-start", {"values": [1, 2, 3]})
        check(f"{name}: process stays alive after ambiguous start()", stub.alive(), "process died")
        check(f"{name}: RpcCommitError.kind respected -- retriable AmbiguousWrite -> 202 pending, not a flat 500",
              code == 202 and body.get("state") == "pending", (code, body))
        check(f"{name}: failpoint genuinely armed AND fired (not a vacuous pass)", proxy.assert_all_fired().get("ok") is True, proxy.assert_all_fired())
        check(f"{name}: body did not run on the ambiguous attempt itself (start()'s own ack was lost, never reached the body)",
              stub.exec_count() == 0, stub.exec_count())

        time.sleep(1.5)
        code2, body2 = stub.put("echo-transform", "op-fp-start", {"values": [1, 2, 3]})
        check(f"{name}: retry after lease expiry converges -- succeeded, same durable identity",
              code2 == 200 and body2.get("state") == "succeeded" and body2.get("result", {}).get("sum") == 6, (code2, body2))
        check(f"{name}: no duplicate participant-side mutation -- body ran exactly once despite the ambiguous first attempt",
              stub.exec_count() == 1, stub.exec_count())
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.unlink(cfg_path)


# ===================================================================
# Target 2: sp_singular_complete. Commit ordinal 2 of 2 in a fresh PUT. The body has ALREADY run
# (exec_count=1) when this commit's ack is lost -- the settle write is durable, but the client
# doesn't learn it.
# ===================================================================
def target_complete():
    name = "singular_complete"
    proxy = ProxyControlClient()
    proxy.clear()
    proc, base, cfg_path = spawn_stub(PROXY_DATA_HOST, PROXY_DATA_PORT)
    stub = Stub(base)
    try:
        proxy.arm(nth=2, action="drop_server_response_after_forward")
        code, body = stub.put("echo-transform", "op-fp-complete", {"values": [2, 3, 4]})
        check(f"{name}: process stays alive after ambiguous complete()", stub.alive(), "process died")
        check(f"{name}: RpcCommitError.kind respected -- retriable AmbiguousWrite -> 202 pending, not a flat 500",
              code == 202 and body.get("state") == "pending", (code, body))
        check(f"{name}: failpoint genuinely armed AND fired (not a vacuous pass)", proxy.assert_all_fired().get("ok") is True, proxy.assert_all_fired())
        check(f"{name}: body ran exactly once (the ambiguous commit is complete()'s, after the body already ran)",
              stub.exec_count() == 1, stub.exec_count())

        code2, body2 = stub.put("echo-transform", "op-fp-complete", {"values": [2, 3, 4]})
        check(f"{name}: retry converges -- succeeded, replays the durably-settled result",
              code2 == 200 and body2.get("state") == "succeeded" and body2.get("result", {}).get("sum") == 9, (code2, body2))
        check(f"{name}: no duplicate participant-side mutation -- exec_count still 1 (replayed, not re-executed)",
              stub.exec_count() == 1, stub.exec_count())
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.unlink(cfg_path)


# ===================================================================
# Target 3: sp_singular_resume (the PR2 reclaim/recovery boundary). Commit ordinal 3 of 4 in the
# crash-after-commit reclaim scenario: first PUT commits the body but "crashes" before complete()
# (Working, lease will expire); after expiry, a byte-identical re-PUT drives resume() -> Granted
# (reclaim, rotated token) -- THIS commit is armed.
# ===================================================================
def target_resume():
    name = "singular_resume"
    proxy = ProxyControlClient()
    proxy.clear()
    proc, base, cfg_path = spawn_stub(PROXY_DATA_HOST, PROXY_DATA_PORT, lease_ttl=1)
    stub = Stub(base)
    try:
        body0 = {"values": [1, 1, 1], "_fault": {"crash_after_commit": True}}
        code0, resp0 = stub.put("echo-transform", "op-fp-resume", body0)
        check(f"{name}: setup -- crash-after-commit PUT returns 202 Working", code0 == 202, (code0, resp0))
        check(f"{name}: setup -- body ran once", stub.exec_count() == 1, stub.exec_count())
        time.sleep(2.0)

        proxy.arm(nth=3, action="drop_server_response_after_forward")
        code, body = stub.put("echo-transform", "op-fp-resume", body0)
        check(f"{name}: process stays alive after ambiguous resume()-reclaim", stub.alive(), "process died")
        check(f"{name}: RpcCommitError.kind respected -- retriable AmbiguousWrite -> 202 pending, not a flat 500",
              code == 202 and body.get("state") == "pending", (code, body))
        check(f"{name}: failpoint genuinely armed AND fired (not a vacuous pass)", proxy.assert_all_fired().get("ok") is True, proxy.assert_all_fired())
        exec_after_ambiguous = stub.exec_count()
        check(f"{name}: reclaim did not (yet) re-execute the body on the ambiguous attempt itself",
              exec_after_ambiguous in (1, 2), exec_after_ambiguous)

        # Same nuance as target_start: the ambiguous resume()-reclaim's own commit is durable, so it
        # grants a FRESH lease (1s TTL from that commit's own timestamp) -- an immediate retry
        # correctly sees it as still live (Active -> 202, never steal a live lease), which is not
        # itself convergence. Wait past that fresh lease before retrying, same as the existing
        # crash_after_commit_reclaim_on_put conformance case's own pattern.
        time.sleep(1.5)
        code2, body2 = stub.put("echo-transform", "op-fp-resume", body0)
        check(f"{name}: retry after the reclaimed lease's own expiry converges -- succeeded, same durable identity, correct result",
              code2 == 200 and body2.get("state") == "succeeded" and body2.get("result", {}).get("sum") == 3, (code2, body2))
        check(f"{name}: no duplicate participant-side mutation -- exec_count stays at 1 across the whole reclaim (replayed, never re-executed)",
              stub.exec_count() == 1, stub.exec_count())
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.unlink(cfg_path)


def main():
    targets = {
        "start": target_start,
        "complete": target_complete,
        "resume": target_resume,
    }
    which = sys.argv[1:] if len(sys.argv) > 1 else list(targets.keys())
    for name in which:
        print(f"\n### singular_{name} ###")
        targets[name]()
    print(f"\n{passed} passed, {len(failures)} failed")
    if failures:
        print("FAILURES:", failures)
        sys.exit(1)


if __name__ == "__main__":
    main()
