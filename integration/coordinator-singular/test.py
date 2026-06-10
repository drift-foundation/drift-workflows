#!/usr/bin/env python3
"""Cross-component integration: Microflows coordinator <-> Singular participant.

This suite spans TWO components and TWO schemas over a real HTTP boundary:
    microflows-runner (coordinator)  -> HTTP -> participant-stub -> Singular
        (microflows schema)                          (singular schema)

It is owned at the repository level (integration/) — NOT inside microflows/ —
because it launches both binaries and depends on both schemas. The justfile
beside this file owns the destructive, isolated setup (reset both schemas, seed
Microflows fixtures, build both binaries); this script owns only orchestration
and assertions.

Verified properties:
  - normal success: workflow completes with the operation result
  - lost-ack recovery: a REAL uncertain outcome — the participant commits then
    drops the response (sleeps past the runner's PUT timeout); the runner sees a
    transport failure and recovers via GET on the stable operation id; the
    workflow still completes
  - idempotent re-run: re-running a completed workflow returns the durable
    result and does NOT re-execute the operation body
  - effectively-once execution: the participant exec-count equals the number of
    distinct operations (2), proving no double execution across recovery
  - non-retryable rejection, durable-request recovery, and inconsistent-
    terminal-state handling, exercised against fixed-id rows seeded by the
    `coordinator-fixtures` Mariachi scenario (no ad-hoc SQL in this harness)

Run via `just test` in this directory (it does the setup), or root `just test`.
Stdlib-only.
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
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]   # drift-workflows/ (integration/<suite>/test.py)
MF = REPO / "microflows"
# Binaries come from the suite's work-dir (compiled from source by `just test`).
# Env override is the normal path; the build/dist fallback supports running the
# harness standalone against an already-built binary during dev.
STUB_BIN = Path(os.environ.get("STUB_BIN",
                MF / "participant-stub" / "build" / "dist" / "bin" / "participant-stub"))
RUNNER_BIN = Path(os.environ.get("RUNNER_BIN",
                  MF / "runner" / "build" / "dist" / "bin" / "microflows-runner"))

MDB = {
    "host": os.environ.get("MDB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MDB_PORT", "34114")),
    "user": os.environ.get("MDB_USER", "root"),
    "password": os.environ.get("MDB_ROOT_PWD", "rootpw"),
}
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}: {detail}")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _mariadb_conn(db):
    return {"backend": "mariadb", **MDB, "database": db,
            "connect_timeout_ms": 3000, "io_timeout_ms": 3000,
            "pool": {"keepalive_interval_ms": 100}}


def _wf_id():
    return uuid.uuid4().hex  # 32 hex chars


def run_runner(runner_cfg, base_url, wf_hex, input_json=None):
    cmd = [str(RUNNER_BIN), "--config", runner_cfg, "--participant-url", base_url, "--workflow-id", wf_hex]
    if input_json is not None:
        cmd += ["--input", input_json]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    try:
        return out.returncode, json.loads(line)
    except json.JSONDecodeError:
        return out.returncode, {"raw_stdout": out.stdout, "stderr": out.stderr}


# Fixed-id fixtures seeded by the `coordinator-fixtures` Mariachi scenario
# (microflows/db/scenarios/coordinator-fixtures/). Each represents a durable DB
# state the normal slice cannot reach by running the coordinator forward, so we
# seed it declaratively (clean reset + overlay) instead of mutating live rows.
WF_RECOVERY = "a0000000000000000000000000000001"   # forward/requested, due; request persisted with a _fault input
WF_COMPLETED_UNSETTLED = "a0000000000000000000000000000002"  # completed, but operation still 'requested' (no result)
WF_COMPLETED_NO_OP = "a0000000000000000000000000000003"      # completed, with NO operation row at all


def _request_count(base):
    with urllib.request.urlopen(f"{base}/debug/request-count", timeout=3) as r:
        return json.loads(r.read())["count"]


def main():
    for b in (STUB_BIN, RUNNER_BIN):
        if not b.exists():
            sys.exit(f"error: missing binary {b} — build it first (`just test` here builds both)")

    sg = f"slice-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    stub_cfg = {"port": port, "service_group": sg, "worker_id": "stub-1", "singular": _mariadb_conn("singular")}
    runner_cfg = {"worker_id": "runner-1", "db": _mariadb_conn("microflows")}

    scf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(stub_cfg, scf); scf.close()
    rcf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(runner_cfg, rcf); rcf.close()

    stub = subprocess.Popen([str(STUB_BIN), "--config", scf.name],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        if stub.poll() is not None:
            sys.exit(f"stub exited early:\n{stub.stdout.read() if stub.stdout else ''}")
        try:
            urllib.request.urlopen(f"{base}/debug/exec-count", timeout=1); break
        except Exception:
            time.sleep(0.2)
    else:
        stub.terminate(); sys.exit("stub not ready")

    try:
        wf_a = _wf_id()
        wf_b = _wf_id()

        # 1. normal success
        code, body = run_runner(rcf.name, base, wf_a)
        check("normal_success", code == 0 and body.get("workflow") == "completed"
              and body.get("result") == {"sum": 6}, (code, body))

        # 2. lost-ack recovery: participant commits then drops the response
        # (sleeps 5s, past the runner's 3s PUT timeout) -> real transport failure
        # -> GET reconcile. Operation executes exactly once.
        lost_input = json.dumps({"values": [1, 2, 3], "_fault": {"delay_after_commit_ms": 5000}})
        code, body = run_runner(rcf.name, base, wf_b, input_json=lost_input)
        check("lost_ack_recovery", code == 0 and body.get("workflow") == "completed"
              and body.get("result") == {"sum": 6}, (code, body))

        # 3. idempotent re-run of an already-completed workflow (no re-execution)
        code, body = run_runner(rcf.name, base, wf_a)
        check("idempotent_rerun", code == 0
              and body.get("workflow") in ("completed", "already_terminal")
              and body.get("result") == {"sum": 6}, (code, body))

        # 3b. 202/pending — INITIAL deferral only. The participant accepts but is
        # not terminal; the runner durably DEFERS (releases the lease) and exits
        # pending, never throwing while holding the lease. No body execution
        # (exec-count unchanged). NOTE: the fixture leaves Singular permanently
        # Working — there is no reclaim or asynchronous-completion path yet, so
        # re-running wf_c would stay pending forever. This asserts ONLY that the
        # first pending deferral is durable, not end-to-end pending recovery.
        wf_c = _wf_id()
        pending_input = json.dumps({"values": [1, 2, 3], "_fault": {"respond_pending": True}})
        code, body = run_runner(rcf.name, base, wf_c, input_json=pending_input)
        check("initial_pending_deferral", code == 9 and body.get("workflow") == "pending", (code, body))

        # 4. effectively-once execution: exactly 2 distinct operations executed.
        with urllib.request.urlopen(f"{base}/debug/exec-count", timeout=3) as r:
            n = json.loads(r.read())["count"]
        check("effectively_once_execution", n == 2, f"exec_count={n} (expected 2)")

        # 4b. definite rejection is NON-RETRYABLE and durably persisted. Invalid
        # input -> participant 400 -> the runner transitions the workflow to
        # blocked_resolution (NOT a silent abort that keeps the lease). The op
        # body never runs (exec-count unchanged at 2).
        wf_d = _wf_id()
        bad_input = json.dumps({"not_values": 1})
        code, body = run_runner(rcf.name, base, wf_d, input_json=bad_input)
        check("rejection_blocks", code == 3 and body.get("workflow") == "failed"
              and body.get("disposition") == "blocked_resolution", (code, body))

        # 4c. the blocked workflow does NOT repeat the rejected dispatch: a re-run
        # finds it non-claimable (blocked_resolution, a non-terminal blocked
        # state) and reports it deferred WITHOUT making a single participant
        # request. Assert the EXACT response and that the participant request
        # count is unchanged across the re-run (no second dispatch occurred).
        rc_before = _request_count(base)
        code, body = run_runner(rcf.name, base, wf_d, input_json=bad_input)
        rc_after = _request_count(base)
        check("rejection_not_repeating",
              code == 5 and body.get("workflow") == "deferred"
              and body.get("reason") == "not_yet_due_or_blocked"
              and rc_after == rc_before,
              (code, body, f"requests {rc_before}->{rc_after}"))

        # 4d. rejection executed NO operation body — exec-count is still 2.
        with urllib.request.urlopen(f"{base}/debug/exec-count", timeout=3) as r:
            n2 = json.loads(r.read())["count"]
        check("rejection_no_execution", n2 == 2, f"exec_count={n2} (expected 2)")

        # 4e. durable-request recovery (seeded fixture WF_RECOVERY): a forward/
        # requested workflow whose persisted operation request carries a _fault
        # flag, seeded due. A resumed worker MUST reuse that durable request, not
        # re-derive it from CLI input — so resuming with a DIFFERENT input still
        # dispatches the persisted (faulting) request and defers pending again
        # (code 9). Re-deriving from CLI input would change the input_hash and
        # raise operation_conflict (code 3). The differing CLI input is the test.
        code, body = run_runner(rcf.name, base, WF_RECOVERY, input_json=json.dumps({"values": [9, 8, 7]}))
        check("durable_request_recovery", code == 9 and body.get("workflow") == "pending"
              and body.get("reason") != "operation_conflict", (code, body))

        # 4f. inconsistent terminal state (seeded fixture): COMPLETED but the
        # operation is still 'requested' (no settled result). The runner must
        # report an ERROR, not a spurious success.
        code, body = run_runner(rcf.name, base, WF_COMPLETED_UNSETTLED)
        check("completed_operation_unsettled",
              code == 10 and body.get("workflow") == "error"
              and body.get("reason") == "completed_operation_unsettled", (code, body))

        # 4g. inconsistent terminal state (seeded fixture): COMPLETED with NO
        # operation row at all. Also an ERROR, not success.
        code, body = run_runner(rcf.name, base, WF_COMPLETED_NO_OP)
        check("completed_without_operation",
              code == 10 and body.get("workflow") == "error"
              and body.get("reason") == "completed_without_operation", (code, body))

        # 5. terminal rerun with the participant DOWN: Microflows replays the
        # LOCAL authoritative result; no dependency on the participant.
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except Exception:
            stub.kill()
        code, body = run_runner(rcf.name, base, wf_a)
        check("terminal_rerun_participant_down", code == 0
              and body.get("workflow") == "already_terminal"
              and body.get("result") == {"sum": 6}, (code, body))
    finally:
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except Exception:
            stub.kill()
        os.unlink(scf.name); os.unlink(rcf.name)

    total = 12
    print(f"coordinator<->singular integration: {total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
