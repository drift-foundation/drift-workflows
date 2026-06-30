#!/usr/bin/env python3
"""Black-box HTTP conformance harness for the Microflows participant stub.

Spawns the built stub binary (Singular-backed) against the running MariaDB
`singular` schema, then drives the operation protocol over real HTTP and
asserts observable behavior. Stdlib-only (no pytest/requests dependency).

Prereqs:
  - `just build` in participant-stub (binary at build/dist/bin/participant-stub)
  - MariaDB up on 127.0.0.1:34214 with the `singular` schema loaded
    (`cd ../../singular && just db-load-schema`)
  - MDB_ROOT_PWD (default: rootpw)

Each run uses a unique service_group, so Singular's immutable terminal records
never collide across runs.
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

ROOT = Path(__file__).resolve().parents[2]  # participant-stub/
BIN = ROOT / "build" / "dist" / "bin" / "participant-stub"


def _free_port() -> int:
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


def spawn_stub(lease_ttl=None):
    if not BIN.exists():
        sys.exit(f"error: stub binary not found at {BIN} — run `just build` first")
    port = _free_port()
    sg = f"spike-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    cfg = {
        "port": port,
        "service_group": sg,
        "worker_id": "harness-worker",
        "singular": {
            "backend": "mariadb",
            "host": os.environ.get("MDB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("MDB_PORT", "34214")),
            "user": os.environ.get("MDB_USER", "root"),
            "password": os.environ.get("MDB_ROOT_PWD", "rootpw"),
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


# ---- cases ----

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def first_submit_replay_and_exec_once(s):
    # First submit runs the body and returns the result.
    code, body = s.put("echo-transform", "op-1", {"values": [1, 2, 3]})
    assert code == 200 and body["state"] == "succeeded", (code, body)
    assert body["result"]["sum"] == 6, body
    # Two replays return the same durable result...
    for _ in range(2):
        code, body = s.put("echo-transform", "op-1", {"values": [1, 2, 3]})
        assert code == 200 and body["result"]["sum"] == 6, (code, body)
    # ...and the operation body executed EXACTLY ONCE (observable instrumentation,
    # not the stored result literal). This is the effectively-once proof.
    assert s.exec_count() == 1, f"exec_count={s.exec_count()} (expected 1)"


@case
def input_conflict(s):
    s.put("echo-transform", "op-c", {"values": [1, 2, 3]})
    code, body = s.put("echo-transform", "op-c", {"values": [1, 2, 3, 4]})
    assert code == 409 and body.get("reason") == "input-conflict", (code, body)


@case
def reordered_keys_no_false_conflict(s):
    # Canonical (lex-ordered) hashing: reordered object keys are the SAME input.
    s.put("echo-transform", "op-r", {"values": [1, 2, 3], "note": "x"})
    code, body = s.put("echo-transform", "op-r", {"note": "x", "values": [1, 2, 3]})
    assert code == 200, f"reordered keys must replay, not 409: {(code, body)}"


@case
def invalid_input_creates_no_operation(s):
    assert s.put("echo-transform", "op-i1", {})[0] == 400
    assert s.put("echo-transform", "op-i2", {"values": "nope"})[0] == 400
    assert s.put("echo-transform", "op-i3", {"values": [1, "x"]})[0] == 400
    # invalid input must create NO operation -> GET is 404.
    assert s.get("echo-transform", "op-i1")[0] == 404


@case
def unknown_operation(s):
    code, body = s.put("other-op", "op-u", {"values": [1]})
    assert code == 400 and body.get("reason") == "unknown-operation", (code, body)


@case
def get_terminal_and_unknown(s):
    s.put("echo-transform", "op-g", {"values": [5, 5]})
    code, body = s.get("echo-transform", "op-g")
    assert code == 200 and body["result"]["sum"] == 10, (code, body)
    assert s.get("echo-transform", "op-missing")[0] == 404


@case
def crash_after_commit_reclaim_on_put(_shared):
    # PR2 dangerous-window recovery (Phase 7 case [12]): the participant commits its side effect and
    # "crashes" before complete(); after the Singular lease expires, a byte-identical PUT RECLAIMS the
    # expired-working op (resume -> Granted, rotated token), reruns idempotently (REPLAYED), and completes —
    # exactly-once. Uses its OWN stub with a SHORT lease TTL so the expiry is fast.
    proc, base, cfg_path = spawn_stub(lease_ttl=1)
    rs = Stub(base)
    try:
        body0 = {"values": [4, 5, 6], "_fault": {"crash_after_commit": True}}
        # 1. First PUT: body commits (runs once), then "crash" before complete -> 202 (Working, not terminal).
        code, body = rs.put("echo-transform", "op-crash", body0)
        assert code == 202, f"crash-after-commit must return 202 (committed, not completed): {(code, body)}"
        assert rs.exec_count() == 1, f"body should have run once: exec_count={rs.exec_count()}"
        # 2. Pre-expiry it is Working (GET read-only -> 202), never terminal.
        assert rs.get("echo-transform", "op-crash")[0] == 202, "op must be Working (not terminal) pre-expiry"
        # 3. Wait for the lease (TTL=1s) to expire.
        time.sleep(2.0)
        # 4. Byte-identical re-PUT drives recovery: resume -> Granted -> rerun (REPLAYED) -> complete -> 200.
        code, body = rs.put("echo-transform", "op-crash", body0)
        assert code == 200 and body.get("state") == "succeeded", f"re-PUT must reclaim+complete: {(code, body)}"
        assert body["result"]["sum"] == 15, body
        # 5. EXACTLY-ONCE: the reclaim re-executed NOTHING (REPLAYED) -> exec_count stays 1.
        assert rs.exec_count() == 1, f"reclaim must not re-execute: exec_count={rs.exec_count()}"
        # 6. Now terminal on a read.
        code, body = rs.get("echo-transform", "op-crash")
        assert code == 200 and body["result"]["sum"] == 15, (code, body)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        os.unlink(cfg_path)


def main():
    proc, base, cfg_path = spawn_stub()
    stub = Stub(base)
    failures = 0
    try:
        for fn in CASES:
            try:
                fn(stub)
                print(f"  PASS  {fn.__name__}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {fn.__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {fn.__name__}: {e!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        os.unlink(cfg_path)
    total = len(CASES)
    print(f"participant-stub conformance: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
