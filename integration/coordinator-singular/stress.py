#!/usr/bin/env python3
"""Microflows coordinator STRESS gate (certification): concurrent-submit recovery race.

Per round, WORKERS runner processes race to drive the SAME workflow id with the SAME operation. The
coordinator's durable identity + the participant's idempotent store must collapse that race to EXACTLY
ONE participant execution — so the global exec-count rises by exactly 1 per round, and at least one
driver reports terminal success. Any double-dispatch (delta != 1), no winner, or a crash fails the gate.

Bounded but REAL: ROUNDS x WORKERS concurrent drivers against a live Singular-backed participant + the
coordinator's MariaDB control state. DB-backed; the justfile holds the shared DB lock + resets both
schemas before this runs. Exit 0 = pass.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

STUB_BIN = os.environ["STUB_BIN"]
RUNNER_BIN = os.environ["RUNNER_BIN"]
MDB = {"host": os.environ.get("DB_HOST", "127.0.0.1"),
       "port": int(os.environ.get("DB_PORT", "34214")),
       "user": os.environ.get("DB_USER", "root"),
       "password": os.environ.get("MDB_ROOT_PWD", "rootpw")}
ROUNDS = int(os.environ.get("MF_STRESS_ROUNDS", "20"))
WORKERS = int(os.environ.get("MF_STRESS_WORKERS", "8"))


def _conn(db):
    return {"backend": "mariadb", **MDB, "database": db, "connect_timeout_ms": 3000,
            "io_timeout_ms": 3000, "pool": {"keepalive_interval_ms": 100}}


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _exec_count(base):
    with urllib.request.urlopen(f"{base}/debug/exec-count", timeout=5) as r:
        return int(json.loads(r.read().decode())["count"])


def _drive(cfg_path, wf, resv):
    cmd = [RUNNER_BIN, "--config", cfg_path, "--workflow-id", wf,
           "--operation", "reserve", "--input", json.dumps({"reservation": resv})]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).returncode
    except Exception:
        return 999


def main():
    sport = _free_port()
    stub_cfg = {"port": sport, "service_group": f"mf-stress-{os.getpid()}", "worker_id": "stub-1",
                "singular": _conn("singular")}
    scf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(stub_cfg, scf); scf.close()
    base = f"http://127.0.0.1:{sport}"
    stub = subprocess.Popen([STUB_BIN, "--config", scf.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = False
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{base}/debug/exec-count", timeout=1); ready = True; break
            except Exception:
                time.sleep(0.2)
        if not ready:
            print("microflows-stress: stub not ready"); return 1

        runner_cfg = {
            "worker_id": "runner-1", "db": _conn("microflows"),
            "participants": [{"id": "ref", "transport": {"kind": "http", "endpoints": [base],
                              "selection": "ordered_failover"}, "auth_profile": None}],
            "operations": [
                {"name": "reserve", "participant": "ref", "schema_version": 1,
                 "compensation": {"operation": "release", "schema_version": 1}},
                {"name": "release", "participant": "ref", "schema_version": 1},
            ],
        }
        rcf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(runner_cfg, rcf); rcf.close()

        for rnd in range(ROUNDS):
            wf = uuid.uuid4().hex
            resv = f"mfstress-{rnd}"
            e0 = _exec_count(base)
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                codes = list(ex.map(lambda _: _drive(rcf.name, wf, resv), range(WORKERS)))
            delta = _exec_count(base) - e0
            if delta != 1:
                print(f"microflows-stress: FAIL round {rnd}: exec-count delta={delta} (expected exactly 1) "
                      f"codes={codes}")
                return 1
            if 0 not in codes:
                print(f"microflows-stress: FAIL round {rnd}: no driver reported terminal success codes={codes}")
                return 1
        print(f"microflows-stress: {ROUNDS} rounds x {WORKERS} concurrent drivers — exactly-once dispatch held")
        return 0
    finally:
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except Exception:
            stub.kill()


if __name__ == "__main__":
    sys.exit(main())
