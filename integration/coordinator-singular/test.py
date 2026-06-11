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


def run_runner(runner_cfg, wf_hex, operation=None, input_json=None):
    # Routing comes from the config registry (no --participant-url). --operation is
    # given only for a FRESH submission; a resume runs on the workflow id alone.
    cmd = [str(RUNNER_BIN), "--config", runner_cfg, "--workflow-id", wf_hex]
    if operation is not None:
        cmd += ["--operation", operation]
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
WF_PINNED_MISMATCH = "a0000000000000000000000000000004"      # forward/requested; request pins schema_version=2
WF_REVERSING = "a0000000000000000000000000000005"            # reversing; 1 active checkpoint, not yet dispatched
WF_REVERSE_LOSTACK = "a0000000000000000000000000000006"      # reversing; compensation input carries a drop-after-commit fault
WF_REVERSE_DISPATCHED = "a0000000000000000000000000000007"   # reversing; compensation binding already persisted (recovery)
WF_REVERSE_NO_ACTIVE = "a0000000000000000000000000000008"    # reversing; checkpoint already reversed (inconsistency)
WF_REVERSE_REJECT = "a0000000000000000000000000000009"       # reversing; compensation is definitely rejected (400)
WF_REVERSE_NOBINDING = "a000000000000000000000000000000a"    # reversing; checkpoint op has no compensation binding
# Sub-step B: multi-checkpoint reversing stacks (two active checkpoints).
WF_REVERSE_STACK = "a000000000000000000000000000000b"        # reversing; seq1+seq2 both active
WF_REVERSE_STACK_MID = "a000000000000000000000000000000c"    # reversing; seq2 reversed, seq1 active (mid-stack restart)
WF_REVERSE_STACK_LOSTACK = "a000000000000000000000000000000d"  # reversing; seq1 (compensated 2nd) drops its ack


def _request_count(base):
    with urllib.request.urlopen(f"{base}/debug/request-count", timeout=3) as r:
        return json.loads(r.read())["count"]


def _exec_count(base):
    with urllib.request.urlopen(f"{base}/debug/exec-count", timeout=3) as r:
        return json.loads(r.read())["count"]


def _put_count(base):
    # PUT-only subset of request-count. A GET-first reconcile leaves this
    # unchanged, so a delta of 0 over a recovery proves no re-PUT occurred.
    with urllib.request.urlopen(f"{base}/debug/put-count", timeout=3) as r:
        return json.loads(r.read())["count"]


def _mdb(sql):
    """Read-only DB introspection for durable-state assertions (NOT seeding — the
    fixtures are still seeded declaratively by the Mariachi scenario). Returns the
    rows as a list of field-lists (tab-separated, batch mode, NULL preserved)."""
    cmd = ["/usr/bin/mariadb", "-h", MDB["host"], "-P", str(MDB["port"]),
           "-u", MDB["user"], f"-p{MDB['password']}", "-N", "-B",
           "microflows", "-e", sql]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(f"mariadb query failed: {out.stderr.strip()}")
    return [line.split("\t") for line in out.stdout.splitlines()]


def _put_op(base, operation, op_id_hex, body_obj):
    """Pre-submit an operation to the participant under a durable id (simulate a
    committed dispatch before a crash, so the runner reconciles on recovery)."""
    req = urllib.request.Request(
        f"{base}/microflows/v1/operations/{operation}/{op_id_hex}",
        data=json.dumps(body_obj).encode(), method="PUT",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def main():
    for b in (STUB_BIN, RUNNER_BIN):
        if not b.exists():
            sys.exit(f"error: missing binary {b} — build it first (`just test` here builds both)")

    sg = f"slice-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    stub_cfg = {"port": port, "service_group": sg, "worker_id": "stub-1", "singular": _mariadb_conn("singular")}
    # Trusted deployment config: a participant registry (the lasting boundary) +
    # an operations registry (op -> participant + pinned schema_version). Both ops
    # route to the one stub endpoint via ordered selection; no auth this slice.
    runner_cfg = {
        "worker_id": "runner-1",
        "db": _mariadb_conn("microflows"),
        "participants": [
            {"id": "ref",
             "transport": {"kind": "http", "endpoints": [base], "selection": "ordered_failover"},
             "auth_profile": None},
        ],
        "operations": [
            {"name": "echo-transform", "participant": "ref", "schema_version": 1},
            {"name": "string-join", "participant": "ref", "schema_version": 1},
            # 'reserve' (a forward op present only as a checkpoint operation_name in the
            # reversing fixtures) declares its compensation -> 'release'; both resolve
            # to the same stub. The runner uses this manual-IR binding to unwind.
            {"name": "reserve", "participant": "ref", "schema_version": 1,
             "compensation": {"operation": "release", "schema_version": 1}},
            {"name": "release", "participant": "ref", "schema_version": 1},
        ],
    }

    scf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(stub_cfg, scf); scf.close()
    rcf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(runner_cfg, rcf); rcf.close()

    stub = subprocess.Popen([str(STUB_BIN), "--config", scf.name],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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

        # 1. normal success — FRESH echo-transform submission (operation + input
        # via CLI; routing resolved from the config registry, never CLI).
        code, body = run_runner(rcf.name, wf_a, "echo-transform", json.dumps({"values": [1, 2, 3]}))
        check("normal_success", code == 0 and body.get("workflow") == "completed"
              and body.get("result") == {"sum": 6}, (code, body))

        # 2. lost-ack recovery: participant commits then drops the response
        # (sleeps 5s, past the runner's 3s PUT timeout) -> real transport failure
        # -> GET reconcile. Operation executes exactly once.
        lost_input = json.dumps({"values": [1, 2, 3], "_fault": {"delay_after_commit_ms": 5000}})
        code, body = run_runner(rcf.name, wf_b, "echo-transform", lost_input)
        check("lost_ack_recovery", code == 0 and body.get("workflow") == "completed"
              and body.get("result") == {"sum": 6}, (code, body))

        # 3. idempotent re-run of an already-completed workflow — RESUME by id alone
        # (no --operation; the durable request drives it; no re-execution).
        code, body = run_runner(rcf.name, wf_a)
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
        code, body = run_runner(rcf.name, wf_c, "echo-transform", pending_input)
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
        code, body = run_runner(rcf.name, wf_d, "echo-transform", bad_input)
        check("rejection_blocks", code == 3 and body.get("workflow") == "failed"
              and body.get("disposition") == "blocked_resolution", (code, body))

        # 4c. the blocked workflow does NOT repeat the rejected dispatch: a RESUME
        # finds it non-claimable (blocked_resolution, a non-terminal blocked state)
        # and reports it deferred WITHOUT making a single participant request.
        # Assert the EXACT response and an unchanged participant request count.
        rc_before = _request_count(base)
        code, body = run_runner(rcf.name, wf_d)
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

        # 4e. GENERIC DISPATCH — a SECOND, distinct operation type (string-join)
        # proves dispatch is data-driven, not renamed echo. The result shape
        # {"joined": ...} differs from echo's {"sum": N}, and the exec-count bump
        # proves the string-join BODY actually ran (not a replayed fixed document).
        wf_sj = _wf_id()
        sj_input = json.dumps({"parts": ["a", "b", "c"], "sep": "-"})
        code, body = run_runner(rcf.name, wf_sj, "string-join", sj_input)
        check("string_join_dispatch", code == 0 and body.get("workflow") == "completed"
              and body.get("result") == {"joined": "a-b-c"}, (code, body))
        with urllib.request.urlopen(f"{base}/debug/exec-count", timeout=3) as r:
            n3 = json.loads(r.read())["count"]
        check("string_join_executed", n3 == 3, f"exec_count={n3} (expected 3)")

        # 4f. durable-request recovery (seeded fixture WF_RECOVERY): a forward/
        # requested workflow whose persisted operation request carries a _fault
        # flag, seeded due. A resume MUST reuse that durable request, not re-derive
        # it from CLI input — so resuming with a DIFFERENT input still dispatches
        # the persisted (faulting) request and defers pending again (code 9).
        # Re-deriving from CLI input would change the input_hash -> operation_conflict.
        code, body = run_runner(rcf.name, WF_RECOVERY, "echo-transform", json.dumps({"values": [9, 8, 7]}))
        check("durable_request_recovery", code == 9 and body.get("workflow") == "pending"
              and body.get("reason") != "operation_conflict", (code, body))

        # 4g. inconsistent terminal state (seeded fixture): COMPLETED but the
        # operation is still 'requested' (no settled result). The runner must
        # report an ERROR, not a spurious success.
        code, body = run_runner(rcf.name, WF_COMPLETED_UNSETTLED)
        check("completed_operation_unsettled",
              code == 10 and body.get("workflow") == "error"
              and body.get("reason") == "completed_operation_unsettled", (code, body))

        # 4h. inconsistent terminal state (seeded fixture): COMPLETED with NO
        # operation row at all. Also an ERROR, not success.
        code, body = run_runner(rcf.name, WF_COMPLETED_NO_OP)
        check("completed_without_operation",
              code == 10 and body.get("workflow") == "error"
              and body.get("reason") == "completed_without_operation", (code, body))

        # 4i. pinned-contract-unavailable -> durable OPERATIONAL deferral (NOT a
        # failure/block). WF_PINNED_MISMATCH's persisted request pins
        # schema_version=2, but the running config only offers echo-transform v1.
        # The runner must DEFER with reason pinned_contract_unavailable (release the
        # lease, preserve the request, stay forward) — not fail or block.
        code, body = run_runner(rcf.name, WF_PINNED_MISMATCH)
        check("pinned_contract_unavailable_defers",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "pinned_contract_unavailable", (code, body))

        # 4j. AUTO-RECOVERY after configuration is restored: a config that offers
        # echo-transform v2 lets the SAME workflow resume and complete on its next
        # due poll. This proves the prior defer RELEASED the lease (re-claimable),
        # PRESERVED the request (it dispatches the durable input -> sum 6), and kept
        # it FORWARD. Poll past the defer backoff (robust to DISPATCH_DEFER_SECONDS).
        cfg2 = json.loads(json.dumps(runner_cfg))
        cfg2["operations"] = [{"name": "echo-transform", "participant": "ref", "schema_version": 2}]
        rcf2 = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(cfg2, rcf2); rcf2.close()
        recovered = None
        rdeadline = time.time() + 12
        while time.time() < rdeadline:
            rcode, rbody = run_runner(rcf2.name, WF_PINNED_MISMATCH)
            if rbody.get("workflow") == "completed":
                recovered = rbody
                break
            time.sleep(0.5)
        os.unlink(rcf2.name)
        check("pinned_contract_recovers",
              recovered is not None and recovered.get("result") == {"sum": 6}, recovered)

        # --- REVERSAL / compensation (sub-step A): a reversing workflow unwinds its
        # checkpoint stack by dispatching the bound compensation ('release') through
        # the GENERIC dispatcher, reaching reversed. ---
        # 6a. normal unwind: one active checkpoint -> dispatch 'release' -> reversed.
        code, body = run_runner(rcf.name, WF_REVERSING)
        check("reverse_to_reversed", code == 0 and body.get("workflow") == "reversed", (code, body))
        # durable: a re-run finds it terminal (reversed) and makes NO new participant
        # request (no re-compensation).
        rc_before = _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSING)
        check("reverse_terminal_idempotent",
              code == 0 and body.get("workflow") == "reversed"
              and _request_count(base) == rc_before, (code, body, "request count changed"))

        # 6b. LOST ACK on the reverse dispatch: 'release' commits then drops the
        # response (5s, past the 3s PUT timeout) -> GET reconcile -> still reversed.
        # Assert the compensation executed EXACTLY ONCE and that >1 request was made
        # (the PUT-that-lost-the-ack followed by the GET reconcile).
        ex0, rq0 = _exec_count(base), _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_LOSTACK)
        ex1, rq1 = _exec_count(base), _request_count(base)
        check("reverse_lost_ack",
              code == 0 and body.get("workflow") == "reversed"
              and ex1 - ex0 == 1 and rq1 - rq0 >= 2, (code, body, f"exec+{ex1-ex0} req+{rq1-rq0}"))

        # 6c. RESTART recovery: a CONSISTENT post-request state — the compensation was
        # already dispatched + committed at the participant under its durable id. The
        # runner must RECONCILE GET-FIRST (no re-execution AND no re-PUT), not perform
        # a fresh dispatch. The pre-submit below is the participant's record of that
        # committed dispatch; the runner then resumes from the durable binding.
        _put_op(base, "release", "0000000000000000000000000000b0d7", {"reservation": "r7"})
        ex0, pu0, rq0 = _exec_count(base), _put_count(base), _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_DISPATCHED)
        ex_d, pu_d, rq_d = _exec_count(base) - ex0, _put_count(base) - pu0, _request_count(base) - rq0
        # put-delta == 0 proves no re-PUT, and request-delta == 1 proves the recovery
        # DID contact the participant — exactly one GET. Together: GET-first
        # reconciliation, not a PUT-first re-dispatch and not zero interaction (which
        # would only prove the runner skipped the participant, not that it reconciled).
        check("reverse_restart_recovery",
              code == 0 and body.get("workflow") == "reversed"
              and ex_d == 0 and pu_d == 0 and rq_d == 1,
              (code, body, f"exec+{ex_d} put+{pu_d} req+{rq_d}"))

        # 6d. INCONSISTENCY: a reversing workflow with no active checkpoint durably
        # DEFERS with an audit reason (lease released) rather than exiting lease-held.
        code, body = run_runner(rcf.name, WF_REVERSE_NO_ACTIVE)
        check("reverse_no_active_checkpoint",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "reverse_no_active_checkpoint", (code, body))

        # 6e. DEFINITE compensation failure: the participant rejects 'release' (400).
        # The runner enters blocked_resolution (reverse direction) with the classified
        # reason; the body never runs and no Singular op is created.
        ex0, rq0 = _exec_count(base), _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_REJECT)
        check("reverse_block_on_rejection",
              code == 3 and body.get("workflow") == "blocked"
              and body.get("direction") == "reverse"
              and body.get("reason") == "participant_invalid_request"
              and _exec_count(base) == ex0, (code, body))
        # DURABLE evidence of the block (not just the runner's response): the workflow
        # is blocked_resolution(3) retaining reverse direction(2), the checkpoint is
        # resolution_required(3), and a compensation_blocked event records the
        # classified reason. These are the documented blocked-entry invariants.
        wf9 = "a0000000000000000000000000000009"
        wf_row = _mdb(f"SELECT state, execution_direction, current_disposition "
                      f"FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wf9}')")
        ck_row = _mdb(f"SELECT reversal_state FROM tb_mf_workflow_checkpoint "
                      f"WHERE workflow_id = UNHEX('{wf9}') AND seq = 1")
        ev_row = _mdb(f"SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.reason')) "
                      f"FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wf9}') "
                      f"AND kind = 'compensation_blocked'")
        check("reverse_block_durable_state",
              wf_row == [["3", "2", "2"]] and ck_row == [["3"]]
              and ev_row == [["participant_invalid_request"]],
              (wf_row, ck_row, ev_row))
        # blocked workflow does NOT redispatch on rerun (non-claimable) -> no new request.
        rq1 = _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_REJECT)
        check("reverse_block_no_redispatch",
              code == 5 and body.get("workflow") == "deferred"
              and _request_count(base) == rq1, (code, body, "redispatched while blocked"))

        # 6f. PRE-dispatch resolution failure: the checkpoint's forward op declares NO
        # compensation -> durable operational DEFERRAL (not a block), lease released.
        code, body = run_runner(rcf.name, WF_REVERSE_NOBINDING)
        check("reverse_no_compensation_binding",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "no_compensation_binding", (code, body))

        # --- SUB-STEP B: multi-checkpoint stack reversal. A reversing workflow with
        # TWO active checkpoints unwinds highest-seq -> lowest, each compensation via
        # its OWN durable binding/input/invocation-id, reaching reversed; recovery
        # works mid-stack and no checkpoint is compensated twice. ---
        # 7a. full unwind in one drive: seq2 then seq1 -> reversed. Compensation runs
        # EXACTLY twice (exec +2, no checkpoint compensated twice), in reverse-seq
        # order, with distinct invocation ids and each its own derived input.
        ex0 = _exec_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_STACK)
        ex_d = _exec_count(base) - ex0
        cps = _mdb(f"SELECT seq, reversal_state, LOWER(HEX(reverse_invocation_id)), "
                   f"JSON_UNQUOTE(JSON_EXTRACT(reverse_input_json,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{WF_REVERSE_STACK}') "
                   f"ORDER BY seq")
        # settle order, read from the audit trail: seq 2 settled (descend, next_seq 1)
        # BEFORE seq 1 (terminal) — proving highest -> lowest compensation order.
        order = _mdb(f"SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.seq')) FROM tb_mf_workflow_event "
                     f"WHERE workflow_id = UNHEX('{WF_REVERSE_STACK}') "
                     f"AND kind = 'compensation_settled' ORDER BY event_seq")
        both_reversed = len(cps) == 2 and cps[0][1] == "2" and cps[1][1] == "2"
        ids_distinct = len(cps) == 2 and cps[0][2] != cps[1][2] and "" not in (cps[0][2], cps[1][2])
        inputs_ok = len(cps) == 2 and cps[0][3] == "b1" and cps[1][3] == "b2"
        check("reverse_stack_unwind",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 2
              and both_reversed and ids_distinct and inputs_ok and order == [["2"], ["1"]],
              (code, body, ex_d, cps, order))
        # 7b. terminal re-run: the fully-reversed stack is terminal — no further
        # compensation (no checkpoint compensated twice across drives).
        ex1, rq1 = _exec_count(base), _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_STACK)
        check("reverse_stack_idempotent",
              code == 0 and body.get("workflow") == "reversed"
              and _exec_count(base) == ex1 and _request_count(base) == rq1, (code, body))

        # 7c. RESTART mid-stack: seq2 was already reversed (a worker settled it then
        # crashed). The authoritative head advances to seq1; resume compensates ONLY
        # seq1 (exec +1) -> reversed. Both checkpoints end reversed.
        ex0 = _exec_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_STACK_MID)
        mid = _mdb(f"SELECT seq, reversal_state FROM tb_mf_workflow_checkpoint "
                   f"WHERE workflow_id = UNHEX('{WF_REVERSE_STACK_MID}') ORDER BY seq")
        check("reverse_stack_restart_midstack",
              code == 0 and body.get("workflow") == "reversed"
              and _exec_count(base) - ex0 == 1 and mid == [["1", "2"], ["2", "2"]],
              (code, body, _exec_count(base) - ex0, mid))

        # 7d. LOST ACK mid-unwind: seq2 compensates cleanly, then seq1's release
        # commits and drops the ack (5s > 3s PUT timeout) -> GET reconcile -> reversed.
        # Effectively-once across the WHOLE stack (exec +2), and the exact wire shape
        # proves the reconcile actually happened: 2 PUTs (seq2, seq1) + 3 operation
        # requests (seq2 PUT, seq1 PUT-that-lost-its-ack, seq1 GET reconcile). Asserting
        # the counts pins the GET path — ignored fault injection would show 2 requests.
        ex0, pu0, rq0 = _exec_count(base), _put_count(base), _request_count(base)
        code, body = run_runner(rcf.name, WF_REVERSE_STACK_LOSTACK)
        ex_d, pu_d, rq_d = _exec_count(base) - ex0, _put_count(base) - pu0, _request_count(base) - rq0
        check("reverse_stack_lost_ack",
              code == 0 and body.get("workflow") == "reversed"
              and ex_d == 2 and pu_d == 2 and rq_d == 3,
              (code, body, f"exec+{ex_d} put+{pu_d} req+{rq_d}"))

        # 5. terminal rerun with the participant DOWN: Microflows replays the
        # LOCAL authoritative result; no dependency on the participant.
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except Exception:
            stub.kill()
        code, body = run_runner(rcf.name, wf_a)
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

    total = 29
    print(f"coordinator<->singular integration: {total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
