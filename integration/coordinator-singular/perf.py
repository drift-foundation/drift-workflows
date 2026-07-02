#!/usr/bin/env python3
"""Microflows coordinator PERF gate (certification): service drive throughput vs a committed baseline.

Measures at the COORDINATOR / service API level with the real participant round trips in the workload
(production behavior): boot the long-running uflowsd over a one-reserve-op manifest, then
time CYCLES sequential workflow submissions (each a full drive -> participant -> terminal). Gate
per-workflow latency (`per_wf_ms`) against a committed, machine-keyed baseline (a 3x tolerance catches
cascading slowdowns while absorbing host variance). A MISSING baseline HARD-FAILS — only
`--update-baseline` records, which is then committed; never auto-minted in a gate run. Exit 0 = pass.
"""
import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASELINE_DIR = ROOT / "perf" / "baselines"
RESULTS_DIR = ROOT / "perf" / "results"
STUB_BIN = os.environ["STUB_BIN"]
SERVICE_BIN = os.environ["SERVICE_BIN"]
MDB = {"host": os.environ.get("DB_HOST", "127.0.0.1"),
       "port": int(os.environ.get("DB_PORT", "34214")),
       "user": os.environ.get("DB_USER", "root"),
       "password": os.environ.get("MDB_ROOT_PWD", "rootpw")}
WARMUP = int(os.environ.get("MF_PERF_WARMUP", "15"))
CYCLES = int(os.environ.get("MF_PERF_CYCLES", "200"))
GATED = "per_wf_ms"
TOLERANCE = 3.0


def _conn(db):
    return {"backend": "mariadb", **MDB, "database": db, "connect_timeout_ms": 3000,
            "io_timeout_ms": 3000, "pool": {"keepalive_interval_ms": 100}}


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _machine_id():
    p = Path("/etc/machine-id")
    return hashlib.sha256((p.read_text().strip() if p.exists() else "unknown").encode()).hexdigest()[:32]


def _wait(url):
    for _ in range(120):
        try:
            urllib.request.urlopen(url, timeout=1); return True
        except Exception:
            time.sleep(0.2)
    return False


def _submit(svc_base, wf, code):
    req = urllib.request.Request(f"{svc_base}/v1/workflows/{wf}/submit?script=reserve",
                                 data=json.dumps({"code": code}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    # one-reserve-op manifest (deployment + a reserve script), participant -> the stub.
    sport = _free_port()
    stub_cfg = {"port": sport, "service_group": f"mf-perf-{os.getpid()}", "worker_id": "stub-1",
                "singular": _conn("singular")}
    scf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(stub_cfg, scf); scf.close()
    base = f"http://127.0.0.1:{sport}"
    stub = subprocess.Popen([STUB_BIN, "--config", scf.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    svc = None
    rc = 1
    try:
        if not _wait(f"{base}/debug/exec-count"):
            print("microflows-perf: stub not ready"); return 1
        mfdir = tempfile.mkdtemp()
        (Path(mfdir) / "reserve.mf").write_text("args { code: string }\nsteps { reserve { reservation: arg code } }\n")
        deployment = {
            "worker_id": "perf-runner", "db": _conn("microflows"),
            "participants": [{"id": "ref", "transport": {"kind": "http", "endpoints": [base],
                              "selection": "ordered_failover"}, "auth_profile": None}],
            "operations": [{"name": "reserve", "participant": "ref", "schema_version": 1,
                            "compensation": {"operation": "release", "schema_version": 1}},
                           {"name": "release", "participant": "ref", "schema_version": 1}],
        }
        manifest = Path(mfdir) / "manifest.json"
        manifest.write_text(json.dumps({"deployment": deployment,
                                        "scripts": [{"name": "reserve", "version": "1.0.0", "path": "reserve.mf", "returns": {}}]}))
        vport = _free_port()
        svc = subprocess.Popen([SERVICE_BIN, "--manifest", str(manifest), "--port", str(vport)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        svc_base = f"http://127.0.0.1:{vport}"
        if not _wait(f"{svc_base}/healthz"):
            print("microflows-perf: service not ready"); return 1

        for i in range(WARMUP):
            _submit(svc_base, uuid.uuid4().hex, f"warm-{i}")
        completed = 0
        t0 = time.monotonic()
        for i in range(CYCLES):
            st, body = _submit(svc_base, uuid.uuid4().hex, f"perf-{i}")
            if st == 200 and body.get("workflow") == "completed":
                completed += 1
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if completed != CYCLES:
            print(f"microflows-perf: only {completed}/{CYCLES} workflows completed (logical failure)"); return 1
        per_wf_ms = round(elapsed_ms / CYCLES, 3)
        metric = {"scenario": "service_reserve_drive", "cycles": completed,
                  "elapsed_ms": round(elapsed_ms, 1), GATED: per_wf_ms}
        print(f"[perf] {metric}")

        mid = _machine_id()
        bfile = BASELINE_DIR / f"{mid}.json"
        base_doc = json.loads(bfile.read_text()) if bfile.exists() else {"machine_id": mid, "scenarios": {}}
        name = metric["scenario"]
        recorded = base_doc.get("scenarios", {}).get(name, {}).get(GATED)
        if args.update_baseline:
            base_doc.setdefault("scenarios", {})[name] = {GATED: per_wf_ms}
            BASELINE_DIR.mkdir(parents=True, exist_ok=True)
            bfile.write_text(json.dumps(base_doc, indent=2) + "\n")
            print(f"[perf] recorded baseline {name}.{GATED}={per_wf_ms} for machine {mid} "
                  f"(commit perf/baselines/{mid}.json)")
            rc = 0
        elif recorded is None:
            print(f"error: no committed perf baseline for '{name}' on machine {mid}.\n"
                  f"  fix: run `just perf --update-baseline` on THIS cert host and COMMIT "
                  f"perf/baselines/{mid}.json.\n  the gate refuses to pass without a committed baseline.",
                  file=sys.stderr)
            rc = 1
        else:
            limit = recorded * TOLERANCE
            ok = per_wf_ms <= limit
            print(f"[perf] {name}.{GATED}={per_wf_ms} baseline={recorded} limit={limit:.1f} "
                  f"-> {'PASS' if ok else 'FAIL'}")
            rc = 0 if ok else 1
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "latest.json").write_text(json.dumps({"machine_id": mid, "metric": metric}, indent=2) + "\n")
        return rc
    finally:
        for p in (svc, stub):
            if p is not None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()


if __name__ == "__main__":
    sys.exit(main())
