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
passed = 0


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
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


def run_runner(runner_cfg, wf_hex, operation=None, input_json=None, arguments=None):
    # Routing comes from the config registry (no --participant-url). For a LEGACY single-op
    # workflow, --operation is given only for a FRESH submission. For a PLANNED workflow,
    # --arguments marks a SUBMISSION (create/reassert with those instance args); omitting it
    # is a RESUME (drive from durable state). A resume runs on the workflow id alone.
    cmd = [str(RUNNER_BIN), "--config", runner_cfg, "--workflow-id", wf_hex]
    if operation is not None:
        cmd += ["--operation", operation]
    if input_json is not None:
        cmd += ["--input", input_json]
    if arguments is not None:
        cmd += ["--arguments", arguments]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    try:
        return out.returncode, json.loads(line)
    except json.JSONDecodeError:
        return out.returncode, {"raw_stdout": out.stdout, "stderr": out.stderr}


def emit_content_hash(runner_cfg):
    """Print the active revision's content_hash for a config via the runner's own algorithm
    (--emit-content-hash; DB-free). Used to assert hash identity properties without
    reimplementing the hash."""
    cmd = [str(RUNNER_BIN), "--config", runner_cfg, "--workflow-id", "0" * 32, "--emit-content-hash"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""


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
# Sub-step C/D: forward MID-PLAN restart seeds (op1 already settled + checkpoint).
WF_FWD_RESUME = "a0000000000000000000000000000020"           # forward; op2 not started (resume runs it)
WF_FWD_FAIL_RESTART = "a0000000000000000000000000000021"     # forward; op2 requested w/ reject fault (restart -> reversal)
WF_FWD_PLAN_PINNED = "a0000000000000000000000000000022"      # forward; pinned to revision-1 [e1,e2] (changed input -> revision_unavailable)
WF_FWD_PLAN_PINNED_B = "a0000000000000000000000000000023"    # ditto (changed schema_version -> revision_unavailable)
WF_FWD_PLAN_PINNED_C = "a0000000000000000000000000000024"    # ditto (changed participant -> revision_unavailable)
WF_FWD_PLAN_VERSION_MISMATCH = "a0000000000000000000000000000025"  # pinned 1.0.0; run as gen 2.0.0 -> revision_unavailable (version alone)
WF_FWD_PLAN_MALFORMED_CFG = "a0000000000000000000000000000026"   # claimable; malformed registry post-claim -> defer + release lease
WF_ARGS_MISSING = "a0000000000000000000000000000028"            # claimable planned; pin OK but NO durable args row (inconsistent) -> defer + release lease
WF_LEGACY_NO_PIN = "a0000000000000000000000000000027"            # claimable legacy (no plan pin); planned resume must release the lease
# Manual-IR (C6): a reversing workflow whose ACTIVE checkpoint is a TAKEN branch op. The
# reverse path reads the checkpoint stack (no pin), so a branch-graph resume unwinds it
# WITHOUT re-evaluating the if. Drives "restart across branch reversal".
WF_REVERSE_BRANCH = "a0000000000000000000000000000030"           # reversing; 1 active checkpoint (taken branch op A)


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
            # 'confirm' takes a reserve RESULT ({"reserved": id}) — lets a cross-branch merge feed a
            # branch reserve's result into a later shared op (C9). Compensable -> 'unconfirm'.
            {"name": "confirm", "participant": "ref", "schema_version": 1,
             "compensation": {"operation": "unconfirm", "schema_version": 1}},
            {"name": "unconfirm", "participant": "ref", "schema_version": 1},
        ],
    }

    scf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(stub_cfg, scf); scf.close()
    rcf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(runner_cfg, rcf); rcf.close()

    # A runner config that carries a forward PLAN (manual IR). The plan supersedes
    # the single --operation path; resume re-reads the same plan + recovers per-seq
    # from durable state. Same registry as rcf, plus the ordered steps.
    plan_cfgs = []
    def plan_cfg(steps, version=None, argument_type=None):
        c = dict(runner_cfg); c["plan"] = steps
        # The loaded plan generation's immutable semantic version (default 1.0.0). A
        # process loads ONE generation; resolution is EXACT-MATCH on (plan_version AND
        # content_hash).
        if version is not None:
            c["plan_version"] = version
        # The script's declared closed-object ARGUMENT TYPE. Submitted --arguments are
        # validated against it; its canonical encoding is part of content_hash. Absent =>
        # the empty-object type {} (encoded as "O{}").
        if argument_type is not None:
            c["argument_type"] = argument_type
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(c, f); f.close()
        plan_cfgs.append(f.name)
        return f.name

    def graph_cfg(graph, argument_type=None):
        # Manual-IR control flow (slice 1): a directly-authored control-flow GRAPH (the "graph"
        # config key) instead of the flat "plan". Same registry; supports operation/if/let/return.
        c = dict(runner_cfg); c["graph"] = graph
        if argument_type is not None:
            c["argument_type"] = argument_type
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(c, f); f.close()
        plan_cfgs.append(f.name)
        return f.name

    def typed_graph_cfg(graph, op_types, argument_type=None):
        # Like graph_cfg, but OVERLAYS declared input_type/result_type onto named operations (a
        # per-test typed contract; the global registry stays untyped). op_types: {op: {input_type,
        # result_type}}. The declared types are type-checked at registry build and folded into the
        # content_hash.
        c = dict(runner_cfg)
        ops = []
        for o in runner_cfg["operations"]:
            o2 = dict(o)
            if o2["name"] in op_types:
                o2.update(op_types[o2["name"]])
            ops.append(o2)
        c["operations"] = ops
        c["graph"] = graph
        if argument_type is not None:
            c["argument_type"] = argument_type
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(c, f); f.close()
        plan_cfgs.append(f.name)
        return f.name

    def lower_source(mf_text):
        # Lower `.mf` SOURCE into a runnable config via the runner's OWN --lower-source frontend
        # (base routing = runner_cfg; the parser emits "plan" + "argument_type" + operation
        # contract overlays). Returns (returncode, config_path); config_path is None when lowering
        # FAILS — so a malformed source surfaces as a nonzero exit AND no emitted config (no
        # config to run => no possible dispatch).
        mff = tempfile.NamedTemporaryFile("w", suffix=".mf", delete=False); mff.write(mf_text); mff.close()
        plan_cfgs.append(mff.name)
        cmd = [str(RUNNER_BIN), "--config", rcf.name, "--workflow-id", "0" * 32, "--lower-source", mff.name]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if out.returncode != 0:
            return out.returncode, None
        line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        try:
            cfg = json.loads(line)
        except json.JSONDecodeError:
            return out.returncode, None
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(cfg, f); f.close()
        plan_cfgs.append(f.name)
        return out.returncode, f.name

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

        # === SUB-STEP C: multi-operation forward PLAN (manual IR). A workflow runs an
        # ordered plan; each compensable success persists its own request/result (own
        # seq + stable id) and creates an active checkpoint. The forward runner BUILDS
        # the checkpoint stack that sub-step B unwound from a seed. ===
        # C1. two-op forward plan -> completed, with a 2-checkpoint stack whose payloads
        # are each op's OWN input, DISTINCT per-seq operation ids, exec exactly twice.
        cplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "c1"}},
                          {"operation": "reserve", "input": {"reservation": "c2"}}])
        wfc = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(cplan, wfc, arguments="{}")
        ex_d = _exec_count(base) - ex0
        ck = _mdb(f"SELECT seq, LOWER(HEX(operation_id)), "
                  f"JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                  f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wfc}') ORDER BY seq")
        wf = _mdb(f"SELECT state, current_disposition FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wfc}')")
        ids_distinct = len(ck) == 2 and ck[0][1] != ck[1][1]
        check("forward_plan_builds_stack",
              code == 0 and body.get("workflow") == "completed" and ex_d == 2
              and wf == [["4", "1"]] and len(ck) == 2
              and ck[0][0] == "1" and ck[1][0] == "2"
              and ck[0][2] == "c1" and ck[1][2] == "c2" and ids_distinct,
              (code, body, ex_d, wf, ck))

        # C1b. STRAIGHT-LINE PARITY through the graph (ir.advance): a fresh 2-op planned workflow
        # is driven entirely by advance (the runner no longer has a flat loop). Externally it is
        # identical to the former flat-plan path — both ops dispatch exactly once (exec +2), each
        # gets a DISTINCT per-seq operation id, each checkpoint carries its own input, and the
        # completion result is the FINAL op's result {reserved:p2} (not op1's, not a unit value).
        slplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "p1"}},
                           {"operation": "reserve", "input": {"reservation": "p2"}}])
        wfsl = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(slplan, wfsl, arguments="{}")
        ex_d = _exec_count(base) - ex0
        ck = _mdb(f"SELECT seq, LOWER(HEX(operation_id)), "
                  f"JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                  f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wfsl}') ORDER BY seq")
        check("graph_straight_line_parity",
              code == 0 and body.get("workflow") == "completed" and ex_d == 2
              and body.get("result") == {"reserved": "p2"}
              and len(ck) == 2 and ck[0][2] == "p1" and ck[1][2] == "p2"
              and ck[0][1] != ck[1][1],
              (code, body, ex_d, ck))

        # C2. RESUME mid-plan THROUGH THE GRAPH (ir.advance): op1 was already settled (+
        # checkpoint) by a worker that crashed. The restart drives the graph — advance(args,
        # settled) replays past the settled op1 and yields ONLY op2 as the next NeedOperation;
        # op1 is NOT re-dispatched (exec +1), op2's checkpoint carries its canonical input (e2),
        # and the completion result is op2's result {reserved:e2} (the final op, not op1).
        rplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "e1"}},
                          {"operation": "reserve", "input": {"reservation": "e2"}}])
        ex0 = _exec_count(base)
        code, body = run_runner(rplan, WF_FWD_RESUME)
        ex_d = _exec_count(base) - ex0
        ck = _mdb(f"SELECT seq, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                  f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{WF_FWD_RESUME}') ORDER BY seq")
        wf = _mdb(f"SELECT state FROM tb_mf_workflow WHERE workflow_id = UNHEX('{WF_FWD_RESUME}')")
        check("forward_plan_resume",
              code == 0 and body.get("workflow") == "completed" and ex_d == 1
              and wf == [["4"]] and ck == [["1", "e1"], ["2", "e2"]]
              and body.get("result") == {"reserved": "e2"},
              (code, body, ex_d, wf, ck))

        # C3. REVISION PINNING (durable IR): wf22 is pinned to revision-1's content_hash
        # (the [e1,e2] IR). Resolving revision 1 from a registry whose IR has CHANGED (a
        # different input) yields a content_hash that no longer matches the pin -> the
        # runner durably defers `revision_unavailable`, NEVER substituting (no op runs).
        # (Editing a live revision's IR is itself a deployment error — §10 revisions are
        # immutable; you add a new revision.)
        wrongplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "e1"}},
                              {"operation": "reserve", "input": {"reservation": "ZZZ"}}])
        ex0 = _exec_count(base)
        code, body = run_runner(wrongplan, WF_FWD_PLAN_PINNED)
        check("forward_plan_conflict",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "revision_unavailable" and _exec_count(base) - ex0 == 0, (code, body))

        # C4. CONTRACT in the content_hash: wf23 (pinned to revision-1 @ reserve sv=1).
        # Resolving revision 1 from a registry that bumps reserve's schema_version (1->2)
        # — same names/inputs — changes the content_hash -> revision_unavailable.
        bumped = dict(runner_cfg)
        bumped["operations"] = [
            {"name": "echo-transform", "participant": "ref", "schema_version": 1},
            {"name": "string-join", "participant": "ref", "schema_version": 1},
            {"name": "reserve", "participant": "ref", "schema_version": 2,
             "compensation": {"operation": "release", "schema_version": 1}},
            {"name": "release", "participant": "ref", "schema_version": 1},
        ]
        bumped["plan"] = [{"operation": "reserve", "input": {"reservation": "e1"}},
                          {"operation": "reserve", "input": {"reservation": "e2"}}]
        _bf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(bumped, _bf); _bf.close(); plan_cfgs.append(_bf.name)
        ex0 = _exec_count(base)
        code, body = run_runner(_bf.name, WF_FWD_PLAN_PINNED_B)
        check("forward_plan_contract_conflict",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "revision_unavailable" and _exec_count(base) - ex0 == 0, (code, body))

        # C4b. PARTICIPANT in the content_hash: wf24. Routing reserve to a DIFFERENT
        # participant (ref2, same endpoint) — identical names/inputs/versions — changes
        # the content_hash -> revision_unavailable (no silent re-routing).
        rerouted = dict(runner_cfg)
        rerouted["participants"] = [
            {"id": "ref", "transport": {"kind": "http", "endpoints": [base], "selection": "ordered_failover"}, "auth_profile": None},
            {"id": "ref2", "transport": {"kind": "http", "endpoints": [base], "selection": "ordered_failover"}, "auth_profile": None},
        ]
        rerouted["operations"] = [
            {"name": "echo-transform", "participant": "ref", "schema_version": 1},
            {"name": "string-join", "participant": "ref", "schema_version": 1},
            {"name": "reserve", "participant": "ref2", "schema_version": 1,
             "compensation": {"operation": "release", "schema_version": 1}},
            {"name": "release", "participant": "ref", "schema_version": 1},
        ]
        rerouted["plan"] = [{"operation": "reserve", "input": {"reservation": "e1"}},
                            {"operation": "reserve", "input": {"reservation": "e2"}}]
        _rf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(rerouted, _rf); _rf.close(); plan_cfgs.append(_rf.name)
        ex0 = _exec_count(base)
        code, body = run_runner(_rf.name, WF_FWD_PLAN_PINNED_C)
        check("forward_plan_participant_conflict",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "revision_unavailable" and _exec_count(base) - ex0 == 0, (code, body))

        # C4c. VERSION in the exact-match: wf25 is pinned at plan_version 1.0.0 with the
        # IDENTICAL [e1,e2] content_hash. A process loading the SAME plan content but as
        # generation 2.0.0 cannot satisfy the pin — exact-match fails on the VERSION alone
        # (semver does NOT authorize substituting another version) -> revision_unavailable,
        # no op runs. This is the breaking-change-via-new-version safety: started workflows
        # stay pinned to their exact version.
        vmplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "e1"}},
                           {"operation": "reserve", "input": {"reservation": "e2"}}], version="2.0.0")
        ex0 = _exec_count(base)
        code, body = run_runner(vmplan, WF_FWD_PLAN_VERSION_MISMATCH)
        check("forward_plan_version_conflict",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "revision_unavailable" and _exec_count(base) - ex0 == 0, (code, body))

        # C4d. MALFORMED registry config AFTER claim must DEFER + RELEASE the lease (no
        # leak): a claimable pinned workflow run with an invalid operations registry claims
        # first, then the post-claim registry build/validate throws — the runner catches it
        # and durably defers revision_unavailable (lease cleared), never exiting still
        # holding the lease. (Unknown participant 'ghost' fails _validate_registry.)
        broken_claim = dict(runner_cfg)
        broken_claim["participants"] = []
        broken_claim["operations"] = [{"name": "reserve", "participant": "ghost", "schema_version": 1}]
        _bcf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(broken_claim, _bcf); _bcf.close(); plan_cfgs.append(_bcf.name)
        ex0 = _exec_count(base)
        code, body = run_runner(_bcf.name, WF_FWD_PLAN_MALFORMED_CFG)
        # batch-mode mariadb renders SQL NULL as the literal "NULL".
        lease = _mdb(f"SELECT lease_owner FROM tb_mf_workflow WHERE workflow_id = UNHEX('{WF_FWD_PLAN_MALFORMED_CFG}')")
        check("forward_malformed_registry_defers_no_lease_leak",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "revision_unavailable"
              and lease == [["NULL"]] and _exec_count(base) - ex0 == 0, (code, body, lease))

        # C4e. MISSING durable arguments is INCONSISTENT durable state, not empty args. A planned
        # workflow always has its args child (create_planned writes it atomically), so a claimable
        # pinned workflow with a matching pin but NO args row must DEFER + RELEASE the lease BEFORE
        # any replay/dispatch — never silently treat missing args as {} and dispatch.
        ex0 = _exec_count(base)
        code, body = run_runner(rplan, WF_ARGS_MISSING)
        lease = _mdb(f"SELECT lease_owner FROM tb_mf_workflow WHERE workflow_id = UNHEX('{WF_ARGS_MISSING}')")
        check("forward_missing_args_defers_no_dispatch_no_lease_leak",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "planned_args_missing"
              and lease == [["NULL"]] and _exec_count(base) - ex0 == 0, (code, body, lease))

        # C5. A SINGLE-operation plan whose only step is NON-compensable is valid (the
        # final step never needs compensation) and completes.
        ncplan = plan_cfg([{"operation": "echo-transform", "input": {"values": [1, 2, 3]}}])
        wfnc = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(ncplan, wfnc, arguments="{}")
        wf = _mdb(f"SELECT state FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wfnc}')")
        check("forward_plan_single_noncompensable",
              code == 0 and body.get("workflow") == "completed"
              and _exec_count(base) - ex0 == 1 and wf == [["4"]], (code, body, wf))

        # C5b. NON-DEFAULT plan NAME (static-review item 1): a process configured with
        # script_name 'checkout-v1' must create + run workflows. A fresh workflow under that
        # name pins (checkout-v1, 1.0.0, ...) and completes — proving the active lookup uses
        # the CONFIGURED plan name, not a hardcoded constant.
        named = dict(runner_cfg)
        named["script_name"] = "checkout-v1"
        named["plan"] = [{"operation": "echo-transform", "input": {"values": [4, 5, 6]}}]
        _named_f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(named, _named_f); _named_f.close(); plan_cfgs.append(_named_f.name)
        wfnm = _wf_id()
        code, body = run_runner(_named_f.name, wfnm, arguments="{}")
        nm = _mdb(f"SELECT script_name FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wfnm}')")
        check("forward_named_plan_completes",
              code == 0 and body.get("workflow") == "completed" and nm == [["checkout-v1"]], (code, body, nm))

        # C5c. ARGUMENT IDENTITY on (re)submission + canonical equivalence (review items 1+3).
        # A planned SUBMISSION carries instance arguments (--arguments). Resubmitting the SAME
        # id with the SAME content in a DIFFERENT key order is canonicalized at the command
        # boundary and is idempotent (never a false workflow_conflict); resubmitting with
        # DIFFERENT content is rejected workflow_conflict (instance arguments are immutable).
        ab_type = {"type": "object", "fields": [
            {"name": "a", "type": {"type": "int"}}, {"name": "b", "type": {"type": "int"}}]}
        aplan = plan_cfg([{"operation": "echo-transform", "input": {"values": [7, 8, 9]}}], argument_type=ab_type)
        wfa = _wf_id()
        code, body = run_runner(aplan, wfa, arguments='{"b":2,"a":1}')   # reordered keys
        check("forward_args_submit_completes",
              code == 0 and body.get("workflow") == "completed", (code, body))
        # resubmit, SAME content, canonical order -> idempotent terminal replay (no conflict).
        code, body = run_runner(aplan, wfa, arguments='{"a":1,"b":2}')
        check("forward_args_reorder_equivalent",
              code == 0 and body.get("workflow") == "already_terminal", (code, body))
        # resubmit, DIFFERENT content -> workflow_conflict (no resume under changed args).
        code, body = run_runner(aplan, wfa, arguments='{"a":1,"b":9}')
        check("forward_args_resubmit_conflict",
              code == 9 and body.get("workflow") == "deferred"
              and body.get("reason") == "workflow_conflict", (code, body))

        # C5d. ARGUMENT-TYPE VALIDATION before creation (Step 1b). A richer declared type
        # exercises every validation axis; a valid object completes, each malformed object is
        # rejected as `invalid_arguments` with NO durable instance written.
        vtype = {"type": "object", "fields": [
            {"name": "id", "type": {"type": "int"}},
            {"name": "name", "type": {"type": "string"}},
            {"name": "tags", "type": {"type": "array", "elem": {"type": "string"}}},
            {"name": "meta", "type": {"type": "object", "fields": [
                {"name": "k", "type": {"type": "int"}}]}},
            {"name": "note", "type": {"type": "optional", "inner": {"type": "string"}}}]}
        vplan = plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=vtype)

        def _submit_args(args_obj):
            return run_runner(vplan, _wf_id(), arguments=json.dumps(args_obj))

        def _check_valid(name, args_obj):
            code, body = _submit_args(args_obj)
            check(name, code == 0 and body.get("workflow") == "completed", (code, body))

        def _check_invalid(name, args_obj):
            code, body = _submit_args(args_obj)
            check(name, code == 2 and body.get("workflow") == "aborted"
                  and body.get("reason") == "invalid_arguments", (code, body))

        _full = {"id": 1, "name": "x", "tags": ["a", "b"], "meta": {"k": 2}, "note": "hi"}
        _check_valid("args_valid_full", _full)
        # valid with the OPTIONAL field absent.
        _check_valid("args_valid_optional_absent", {"id": 1, "name": "x", "tags": [], "meta": {"k": 2}})
        # valid with fields in a DIFFERENT order (canonicalized; still conforms).
        _check_valid("args_valid_reordered", {"note": "hi", "meta": {"k": 2}, "tags": ["a"], "name": "x", "id": 1})
        _check_invalid("args_missing_field", {"name": "x", "tags": [], "meta": {"k": 2}})           # no 'id'
        _check_invalid("args_extra_field", {**_full, "extra": 1})                                    # undeclared field
        _check_invalid("args_wrong_scalar", {"id": "nope", "name": "x", "tags": [], "meta": {"k": 2}})  # id not int
        _check_invalid("args_nested_object_mismatch", {"id": 1, "name": "x", "tags": [], "meta": {"k": "no"}})  # meta.k not int
        _check_invalid("args_array_element_mismatch", {"id": 1, "name": "x", "tags": [1, 2], "meta": {"k": 2}})  # tags not strings
        # A rejected submission writes NO durable state: query the workflow + args tables.
        wf_inv = _wf_id()
        code, body = run_runner(vplan, wf_inv, arguments=json.dumps({"name": "x", "tags": [], "meta": {"k": 2}}))  # missing id
        inv_wf = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wf_inv}')")
        inv_args = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_args WHERE workflow_id = UNHEX('{wf_inv}')")
        check("args_invalid_no_durable_state",
              code == 2 and body.get("reason") == "invalid_arguments"
              and inv_wf == [["0"]] and inv_args == [["0"]], (code, body, inv_wf, inv_args))

        # C5e. EXACT NUMERIC semantics (no implicit coercion). Int = integer-shaped + in range;
        # Float = float-shaped + finite.
        ntype = {"type": "object", "fields": [
            {"name": "id", "type": {"type": "int"}}, {"name": "ratio", "type": {"type": "float"}}]}
        nplan = plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=ntype)

        def _nsubmit(raw):  # raw JSON string (so we can send 1e400 etc.)
            return run_runner(nplan, _wf_id(), arguments=raw)

        code, body = _nsubmit('{"id":1,"ratio":1.5}')
        check("args_num_valid", code == 0 and body.get("workflow") == "completed", (code, body))
        for nm, raw in [
            ("args_int_rejects_float_shaped", '{"id":1.0,"ratio":1.5}'),
            ("args_int_rejects_overflow", '{"id":99999999999999999999,"ratio":1.5}'),
            ("args_float_rejects_integer_shaped", '{"id":1,"ratio":2}'),
            ("args_float_rejects_non_finite", '{"id":1,"ratio":1e400}'),
        ]:
            code, body = _nsubmit(raw)
            check(nm, code == 2 and body.get("reason") == "invalid_arguments", (nm, code, body))

        # C5f. UNKNOWN KEY in a type/field declaration is rejected (a typo must not silently
        # produce an unintended contract whose dropped data is absent from content_hash). The
        # malformed argument_type fails registry build, so the submission does not succeed.
        bad_type = {"type": "object", "fields": [
            {"name": "a", "type": {"type": "int", "bogus": 1}}]}   # unknown key "bogus"
        badplan = plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=bad_type)
        wf_bad = _wf_id()
        code, body = run_runner(badplan, wf_bad, arguments='{"a":1}')
        bad_plan_rows = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_plan WHERE workflow_id = UNHEX('{wf_bad}')")
        check("args_type_unknown_key_rejected",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and bad_plan_rows == [["0"]], (code, body, bad_plan_rows))

        # C5g. DECLARATION-ORDER hash-equivalence: the type's canonical encoding sorts fields,
        # so two configs differing ONLY in field order produce the SAME content_hash.
        t_ab = {"type": "object", "fields": [
            {"name": "a", "type": {"type": "int"}}, {"name": "b", "type": {"type": "int"}}]}
        t_ba = {"type": "object", "fields": [
            {"name": "b", "type": {"type": "int"}}, {"name": "a", "type": {"type": "int"}}]}
        wf_ab = _wf_id(); wf_ba = _wf_id()
        run_runner(plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=t_ab),
                   wf_ab, arguments='{"a":1,"b":2}')
        run_runner(plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=t_ba),
                   wf_ba, arguments='{"a":1,"b":2}')
        h_ab = _mdb(f"SELECT LOWER(HEX(content_hash)) FROM tb_mf_workflow_plan WHERE workflow_id = UNHEX('{wf_ab}')")
        h_ba = _mdb(f"SELECT LOWER(HEX(content_hash)) FROM tb_mf_workflow_plan WHERE workflow_id = UNHEX('{wf_ba}')")
        check("args_type_declaration_order_hash_equivalent",
              h_ab == h_ba and h_ab and h_ab != [["NULL"]], (h_ab, h_ba))

        # C5i. CONTENT_HASH tracks graph SEMANTICS, not raw JSON spelling. Two plans whose only
        # difference is the KEY ORDER of a step's input object must emit the SAME content_hash
        # (inputs are canonicalized into each EConst); a real semantic change (a different input
        # value) must emit a DIFFERENT one. Computed via the runner's own --emit-content-hash, so
        # the assertion exercises the actual hashing algorithm rather than a reimplementation.
        cfg_ko1 = plan_cfg([{"operation": "reserve", "input": {"alpha": 1, "beta": 2}}])
        cfg_ko2 = plan_cfg([{"operation": "reserve", "input": {"beta": 2, "alpha": 1}}])
        cfg_sem = plan_cfg([{"operation": "reserve", "input": {"alpha": 1, "beta": 3}}])
        h_ko1, h_ko2, h_sem = emit_content_hash(cfg_ko1), emit_content_hash(cfg_ko2), emit_content_hash(cfg_sem)
        check("content_hash_input_key_order_insensitive",
              len(h_ko1) == 66 and h_ko1 == h_ko2, (h_ko1, h_ko2))
        check("content_hash_changes_on_semantic_graph_change",
              len(h_sem) == 66 and h_sem != h_ko1, (h_sem, h_ko1))

        # ===== C6. MANUAL-IR CONTROL FLOW, slice 1: `if` =====
        # A directly-authored branch graph (the "graph" config key — no parser/DSL): if args.flag
        # then reserve "brA" else reserve "brB", both returning. This drives the graph interpreter
        # (ir.advance) beyond the degenerate straight line. Each branch runs EXACTLY one remote op;
        # the untaken branch is never dispatched. (Both branches have 1 op, so op_depth is uniform
        # = plan_length = 1; the build rejects op-unbalanced branches — C6f.)
        BRANCH_AT = {"type": "object", "fields": [{"name": "flag", "type": {"type": "bool"}}]}

        def branch_graph(fault_on_a=None):
            a_input = {"reservation": "brA"}
            if fault_on_a is not None:
                a_input["_fault"] = fault_on_a
            return {"entry": "n0", "nodes": [
                {"kind": "if", "id": "n0", "cond": {"arg": ["flag"]}, "then": "n1", "else": "n2"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": a_input}, "next": "n3"},
                {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "brB"}}, "next": "n3"},
                {"kind": "return", "id": "n3", "value": {"const": None}},
            ]}

        # A two-deep branch graph for reversal coverage: if args.flag then reserve brA else
        # reserve brB, then a SHARED final op (reserve "fin"), then return. Both branch paths run
        # the branch op THEN the final op -> uniform op_depth = plan_length = 2. The branch op is
        # NON-final (compensable: reserve->release); the final op is the one driven to reject. The
        # merge node's input is CONST (no cross-branch result merge). With fail_final, the final op
        # is rejected -> reversal compensates ONLY the taken branch's checkpoint.
        def rev_branch_graph(fail_final=False):
            fin = {"reservation": "fin"}
            if fail_final:
                fin = {"reservation": "fin", "_fault": {"reject": True}}
            return {"entry": "n0", "nodes": [
                {"kind": "if", "id": "n0", "cond": {"arg": ["flag"]}, "then": "n1", "else": "n2"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "brA"}}, "next": "n3"},
                {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "brB"}}, "next": "n3"},
                {"kind": "operation", "id": "n3", "operation": "reserve", "input": {"const": fin}, "next": "n4"},
                {"kind": "return", "id": "n4", "value": {"const": None}},
            ]}

        gcfg = graph_cfg(branch_graph(), argument_type=BRANCH_AT)

        # C6a. TRUE branch dispatches ONLY op A (reserve brA); op B is never dispatched (exec +1).
        wft = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(gcfg, wft, arguments=json.dumps({"flag": True}))
        ex_d = _exec_count(base) - ex0
        ckt = _mdb(f"SELECT seq, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wft}') ORDER BY seq")
        check("graph_if_true_dispatches_only_branch_a",
              code == 0 and body.get("workflow") == "completed" and body.get("result") == {"reserved": "brA"}
              and ex_d == 1 and ckt == [["1", "brA"]], (code, body, ex_d, ckt))

        # C6b. NO durable record at the branch boundary: the NIf is PURE (replayed, never
        # persisted). A 1-op straight-line plan and this 1-op-on-taken-path branch produce the
        # SAME event count — the branch contributes zero events / continuations.
        slcfg = plan_cfg([{"operation": "reserve", "input": {"reservation": "sl1"}}])
        wfsl1 = _wf_id()
        run_runner(slcfg, wfsl1, arguments="{}")
        nev_t = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wft}')")
        nev_sl = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wfsl1}')")
        check("graph_if_no_durable_record_at_branch_boundary",
              nev_t == nev_sl and nev_t != [["0"]], (nev_t, nev_sl))

        # C6c. FALSE branch dispatches ONLY op B (reserve brB); op A is never dispatched (exec +1).
        wff = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(gcfg, wff, arguments=json.dumps({"flag": False}))
        ex_d = _exec_count(base) - ex0
        ckf = _mdb(f"SELECT seq, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wff}') ORDER BY seq")
        check("graph_if_false_dispatches_only_branch_b",
              code == 0 and body.get("workflow") == "completed" and body.get("result") == {"reserved": "brB"}
              and ex_d == 1 and ckf == [["1", "brB"]], (code, body, ex_d, ckf))

        # C6d. TERMINAL replay of a completed branch workflow returns the durable FINAL op result
        # (the taken branch's), independent of the participant.
        code, body = run_runner(gcfg, wft)
        check("graph_if_terminal_replay_durable_result",
              code == 0 and body.get("workflow") == "already_terminal"
              and body.get("result") == {"reserved": "brA"}, (code, body))

        # C6e. A CLAIMABLE RESUME re-derives the branch through ir.advance from the DURABLE args
        # (args_get), not {} or CLI. Submit flag:true with op A PENDING (respond_pending): branch
        # true is chosen, op A (reserve brA) is requested-not-settled, and the workflow defers.
        # NUDGE next_attempt_at into the past (making it claimable) and RESUME (no --arguments): it
        # actually re-claims, replays the branch from the durable args, and reconciles op A —
        # staying "pending". Had the resume used {}/CLI args, the cond (arg "flag") would be MISSING
        # and replay would FAULT -> deferred:graph_replay_fault, not pending. So a durable-args
        # regression flips this from "pending" to "deferred". (False-branch contrast: flag:false
        # selects op B under the SAME config — the branch is decided by args, not the config.)
        pgcfg = graph_cfg(branch_graph(fault_on_a={"respond_pending": True}), argument_type=BRANCH_AT)
        wf_pt = _wf_id()
        code_s, body_s = run_runner(pgcfg, wf_pt, arguments=json.dumps({"flag": True}))
        op_s = _mdb(f"SELECT operation_name, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
                    f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wf_pt}') ORDER BY operation_seq")
        # Test-control nudge (NOT seeding): make the deferred workflow due so the resume re-claims.
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' "
             f"WHERE workflow_id = UNHEX('{wf_pt}')")
        code_r, body_r = run_runner(pgcfg, wf_pt)   # claimable RESUME -> _run_forward/ir.advance
        op_r = _mdb(f"SELECT operation_name, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
                    f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wf_pt}') ORDER BY operation_seq")
        wf_pf = _wf_id()
        code_f, body_f = run_runner(graph_cfg(branch_graph(), argument_type=BRANCH_AT), wf_pf, arguments=json.dumps({"flag": False}))
        op_f = _mdb(f"SELECT operation_name, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
                    f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wf_pf}') ORDER BY operation_seq")
        check("graph_if_resume_replays_branch_from_durable_args",
              code_s == 9 and body_s.get("workflow") == "pending" and op_s == [["reserve", "brA"]]
              and code_r == 9 and body_r.get("workflow") == "pending" and op_r == [["reserve", "brA"]]
              and code_f == 0 and body_f.get("workflow") == "completed"
              and body_f.get("result") == {"reserved": "brB"} and op_f == [["reserve", "brB"]],
              (code_s, body_s, op_s, code_r, body_r, op_r, code_f, body_f, op_f))

        # C6f. An op-UNBALANCED if (true path 2 ops, false path 1) is NON-EXECUTABLE: rejected at
        # BUILD (op_depth), surfaced as invalid_config, with NO operation dispatched.
        unbal_graph = {"entry": "n0", "nodes": [
            {"kind": "if", "id": "n0", "cond": {"arg": ["flag"]}, "then": "n1", "else": "n2"},
            {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "u1"}}, "next": "n4"},
            {"kind": "operation", "id": "n4", "operation": "reserve", "input": {"const": {"reservation": "u2"}}, "next": "n3"},
            {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "u3"}}, "next": "n3"},
            {"kind": "return", "id": "n3", "value": {"const": None}},
        ]}
        ubcfg = graph_cfg(unbal_graph, argument_type=BRANCH_AT)
        wfu = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(ubcfg, wfu, arguments=json.dumps({"flag": True}))
        check("graph_unbalanced_branch_rejected_before_dispatch",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C6g. A graph with a NON-COMPENSABLE NON-FINAL operation is rejected at BUILD: op0
        # (echo-transform, no compensation binding) precedes the branch, so a later op could fail
        # and strand reversal. Rejected as invalid_config, with no operation dispatched.
        noncomp_graph = {"entry": "n0", "nodes": [
            {"kind": "operation", "id": "n0", "operation": "echo-transform", "input": {"const": {"values": [1]}}, "next": "n1"},
            {"kind": "if", "id": "n1", "cond": {"arg": ["flag"]}, "then": "n2", "else": "n3"},
            {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "x"}}, "next": "n4"},
            {"kind": "operation", "id": "n3", "operation": "reserve", "input": {"const": {"reservation": "y"}}, "next": "n4"},
            {"kind": "return", "id": "n4", "value": {"const": None}},
        ]}
        ncg = graph_cfg(noncomp_graph, argument_type=BRANCH_AT)
        wfnc2 = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(ncg, wfnc2, arguments=json.dumps({"flag": True}))
        check("graph_noncompensable_nonfinal_op_rejected",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C6h. FORWARD FAILURE after a taken branch: the taken branch's compensable op (reserve
        # brA) settles + checkpoints, then the SHARED final op (reserve fin) is definitely rejected
        # (400) -> begin_reversal -> compensate ONLY the taken path's checkpoint (release brA) ->
        # reversed. exec = reserve brA + release brA = 2 (the rejected final op runs no body). The
        # UNTAKEN branch (brB) has NO operation row, NO checkpoint, NO participant execution.
        rbcfg = graph_cfg(rev_branch_graph(fail_final=True), argument_type=BRANCH_AT)
        wfrb = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(rbcfg, wfrb, arguments=json.dumps({"flag": True}))
        ex_d = _exec_count(base) - ex0
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wfrb}')")
        cks = _mdb(f"SELECT seq, reversal_state, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wfrb}') ORDER BY seq")
        ops = _mdb(f"SELECT operation_seq, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
                   f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wfrb}') ORDER BY operation_seq")
        brB_ops = _mdb(f"SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id = UNHEX('{wfrb}') AND input_json LIKE '%brB%'")
        brB_cks = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wfrb}') AND payload LIKE '%brB%'")
        check("graph_branch_forward_fail_reverses_taken_path_only",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 2
              and wf == [["5", "2"]] and cks == [["1", "2", "brA"]]
              and ops == [["1", "brA"], ["2", "fin"]]
              and brB_ops == [["0"]] and brB_cks == [["0"]],
              (code, body, ex_d, wf, cks, ops, brB_ops, brB_cks))

        # C6i. MID-FLIGHT branch resume reconciles the persisted request GET-FIRST (no second PUT).
        # Submit flag:true with branch op A PENDING: op A is requested-not-settled (a durable
        # operation_request, no checkpoint; Singular left Working). A claimable RESUME RECOVERS that
        # request and reconciles it by GET (the participant is still Working -> 202 pending) — it
        # does NOT re-PUT the same operation id (put delta 0), issues at least the reconcile GET
        # (request delta >= 1), and never re-evaluates into the other branch (the op row is
        # unchanged: still the single seq-1 reserve brA).
        mfcfg = graph_cfg(branch_graph(fault_on_a={"respond_pending": True}), argument_type=BRANCH_AT)
        wfmf = _wf_id()
        opq = (f"SELECT operation_seq, operation_name, status, "
               f"JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
               f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wfmf}') ORDER BY operation_seq")
        code_s, body_s = run_runner(mfcfg, wfmf, arguments=json.dumps({"flag": True}))
        op_s = _mdb(opq)
        # Test-control nudge (NOT seeding): make the deferred workflow due so the resume re-claims.
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' "
             f"WHERE workflow_id = UNHEX('{wfmf}')")
        pu0, rq0 = _put_count(base), _request_count(base)
        code_r, body_r = run_runner(mfcfg, wfmf)   # claimable RESUME -> GET-first reconcile
        pu_d, rq_d = _put_count(base) - pu0, _request_count(base) - rq0
        op_r = _mdb(opq)
        check("graph_branch_midflight_resume_get_first_no_second_put",
              code_s == 9 and body_s.get("workflow") == "pending"
              and code_r == 9 and body_r.get("workflow") == "pending"
              and pu_d == 0 and rq_d >= 1
              and op_s == [["1", "reserve", "1", "brA"]] and op_r == op_s,
              (code_s, body_s, code_r, body_r, f"put+{pu_d} req+{rq_d}", op_s, op_r))

        # C6j. RESTART across branch reversal: a worker took the branch, settled the branch op
        # (durable checkpoint, reservation brA), and BEGAN reversal, then crashed (seeded
        # WF_REVERSE_BRANCH: state=reversing, direction=reverse, one active checkpoint). A fresh
        # resume driven with the BRANCH graph config unwinds from the CHECKPOINT STACK, not graph
        # control flow: it never re-evaluates the if (a graph re-eval would dispatch a forward
        # reserve, so exec would exceed 1), compensates the one checkpoint EXACTLY ONCE via
        # 'release' on its durable payload (brA), and reaches reversed in the reverse direction. The
        # graph IS present (its pin must verify), but the reverse path never consults it. The seed
        # is transition-faithful (reversal_begun audit head matching current_event_seq, an args
        # child for the planned invariant); only the plan pin is inserted here, because its
        # content_hash is the runner's own digest of this exact config (asserted live, not frozen).
        rbrcfg = graph_cfg(rev_branch_graph(), argument_type=BRANCH_AT)
        _mdb(f"INSERT INTO tb_mf_workflow_plan (workflow_id, plan_version, content_hash, plan_length, created_at) "
             f"VALUES (UNHEX('{WF_REVERSE_BRANCH}'), '1.0.0', UNHEX('{emit_content_hash(rbrcfg)}'), 2, '2000-01-01 00:00:00.000000')")
        ex0 = _exec_count(base)
        code, body = run_runner(rbrcfg, WF_REVERSE_BRANCH)
        ex_d = _exec_count(base) - ex0
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{WF_REVERSE_BRANCH}')")
        ck = _mdb(f"SELECT reversal_state, reverse_operation_name, "
                  f"JSON_UNQUOTE(JSON_EXTRACT(reverse_input_json,'$.reservation')) "
                  f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{WF_REVERSE_BRANCH}') ORDER BY seq")
        check("graph_branch_reversal_restart_unwinds_from_checkpoint",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 1
              and wf == [["5", "2"]] and ck == [["2", "release", "brA"]],
              (code, body, ex_d, wf, ck))

        # ===== C7. MANUAL-IR FINITE ARRAY EXPRESSIONS: map/filter/fold (NLoop) feeding an op =====
        # A loop is a PURE value step (no operation seq, no durable write): it is recomputed on
        # replay from durable args/results, never persisted. Here a fold selects an element of a
        # finite array (the body is a pure IrExpr — it cannot dispatch), and that loop result feeds
        # a remote operation's input. (fold-select-last is the one expression that produces a whole
        # operation-input object; map/filter/fold execution itself is unit-covered in ir_exec_test.)
        CAND_AT = {"type": "object", "fields": [{"name": "candidates", "type": {"type": "array",
                   "elem": {"type": "object", "fields": [{"name": "reservation", "type": {"type": "string"}}]}}}]}

        def loop_fold_graph():
            # fold over args.candidates, threading the element into `chosen` -> chosen = LAST element;
            # reserve(chosen); return its result. The fold result is the operation input.
            return {"entry": "n0", "nodes": [
                {"kind": "fold", "id": "n0", "source": {"arg": ["candidates"]}, "elem": "c", "as": "chosen",
                 "init": {"const": None}, "body": {"local": {"name": "c"}}, "next": "n1"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"local": {"name": "chosen"}}, "next": "n2"},
                {"kind": "return", "id": "n2", "value": {"result": {"node": "n1"}}},
            ]}

        # C7a. The loop computes the operation input (selects the last candidate), then the remote op
        # dispatches EXACTLY once with that derived input.
        wla = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(graph_cfg(loop_fold_graph(), argument_type=CAND_AT), wla,
                                arguments=json.dumps({"candidates": [{"reservation": "a"}, {"reservation": "b"}, {"reservation": "c"}]}))
        ex_d = _exec_count(base) - ex0
        ops = _mdb(f"SELECT operation_seq, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
                   f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wla}') ORDER BY operation_seq")
        check("graph_loop_computes_op_input_dispatches_once",
              code == 0 and body.get("workflow") == "completed" and body.get("result") == {"reserved": "c"}
              and ex_d == 1 and ops == [["1", "c"]], (code, body, ex_d, ops))

        # C7b. NO durable event at the loop boundary: the fold is PURE (replayed, never persisted).
        # The loop graph and an equivalent CONST-input reserve produce the SAME event count — the
        # loop contributes zero events / continuations.
        const_graph = {"entry": "n0", "nodes": [
            {"kind": "operation", "id": "n0", "operation": "reserve", "input": {"const": {"reservation": "c"}}, "next": "n1"},
            {"kind": "return", "id": "n1", "value": {"result": {"node": "n0"}}},
        ]}
        wlc = _wf_id()
        run_runner(graph_cfg(const_graph), wlc, arguments="{}")
        nev_loop = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wla}')")
        nev_const = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wlc}')")
        check("graph_loop_no_durable_event_at_loop_boundary",
              nev_loop == nev_const and nev_loop != [["0"]], (nev_loop, nev_const))

        # C7c. RESUME: a SETTLED prior op plus a loop-derived input recomputes IDENTICALLY. op1's
        # input is fold-derived (the last candidate); it settles + checkpoints. op2 (a const-input
        # reserve) is PENDING, so the workflow defers. A claimable RESUME recomputes the fold from
        # the DURABLE args — re-deriving op1's input identically (op1 stays settled, never
        # re-dispatched) — and reconciles op2 GET-first (no second PUT). A non-deterministic loop
        # would change op1's recorded input or re-dispatch it; this asserts neither happens.
        def loop_resume_graph():
            return {"entry": "n0", "nodes": [
                {"kind": "fold", "id": "n0", "source": {"arg": ["candidates"]}, "elem": "c", "as": "chosen",
                 "init": {"const": None}, "body": {"local": {"name": "c"}}, "next": "n1"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"local": {"name": "chosen"}}, "next": "n2"},
                {"kind": "operation", "id": "n2", "operation": "reserve",
                 "input": {"const": {"reservation": "q", "_fault": {"respond_pending": True}}}, "next": "n3"},
                {"kind": "return", "id": "n3", "value": {"result": {"node": "n2"}}},
            ]}
        rcfg = graph_cfg(loop_resume_graph(), argument_type=CAND_AT)
        wlr = _wf_id()
        opq = (f"SELECT operation_seq, status, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
               f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wlr}') ORDER BY operation_seq")
        code_s, body_s = run_runner(rcfg, wlr, arguments=json.dumps({"candidates": [{"reservation": "a"}, {"reservation": "b"}, {"reservation": "z"}]}))
        op_s = _mdb(opq)
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' WHERE workflow_id = UNHEX('{wlr}')")
        pu0, rq0 = _put_count(base), _request_count(base)
        code_r, body_r = run_runner(rcfg, wlr)   # claimable RESUME -> recompute fold + reconcile op2
        pu_d, rq_d = _put_count(base) - pu0, _request_count(base) - rq0
        op_r = _mdb(opq)
        # op1 settled(status 2) with the fold-derived input "z" (the last candidate); op2 requested(1)
        # with the const "q". After resume: identical rows (loop recomputed deterministically).
        check("graph_loop_resume_recomputes_loop_derived_input_identically",
              code_s == 9 and body_s.get("workflow") == "pending"
              and op_s == [["1", "2", "z"], ["2", "1", "q"]]
              and code_r == 9 and body_r.get("workflow") == "pending" and op_r == op_s
              and pu_d == 0 and rq_d >= 1,
              (code_s, body_s, op_s, code_r, body_r, op_r, f"put+{pu_d} req+{rq_d}"))

        # ===== C8. MANUAL-IR CONTROL FLOW: `case` (NCase from graph config) =====
        # A directly-authored multi-way branch: case args.mode -> arm "a"/"b" else default, each
        # reserving a distinct id. Pure control flow (the NCase is replayed, never persisted); the
        # interpreter + validation + op_depth already supported NCase — this exercises the config
        # surface. Each arm/default runs EXACTLY one op (uniform op_depth = plan_length = 1).
        CASE_AT = {"type": "object", "fields": [{"name": "mode", "type": {"type": "string"}}]}

        def case_graph(fault_on_a=None):
            a_input = {"reservation": "cmA"}
            if fault_on_a is not None:
                a_input["_fault"] = fault_on_a
            return {"entry": "n0", "nodes": [
                {"kind": "case", "id": "n0", "scrutinee": {"arg": ["mode"]},
                 "arms": [{"match": "a", "target": "n1"}, {"match": "b", "target": "n2"}], "default": "n3"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": a_input}, "next": "n4"},
                {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "cmB"}}, "next": "n4"},
                {"kind": "operation", "id": "n3", "operation": "reserve", "input": {"const": {"reservation": "cmD"}}, "next": "n4"},
                {"kind": "return", "id": "n4", "value": {"const": None}},
            ]}
        ccfg = graph_cfg(case_graph(), argument_type=CASE_AT)

        # C8a. The matching arm is selected from the DURABLE args: mode "a" dispatches ONLY op A
        # (reserve cmA); mode "b" ONLY op B. The untaken arms/default never dispatch (exec +1 each).
        wca = _wf_id()
        ex0 = _exec_count(base)
        code_a, body_a = run_runner(ccfg, wca, arguments=json.dumps({"mode": "a"}))
        exa = _exec_count(base) - ex0
        cka = _mdb(f"SELECT seq, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wca}') ORDER BY seq")
        wcb = _wf_id()
        ex0 = _exec_count(base)
        code_b, body_b = run_runner(ccfg, wcb, arguments=json.dumps({"mode": "b"}))
        exb = _exec_count(base) - ex0
        check("graph_case_selects_matching_arm",
              code_a == 0 and body_a.get("result") == {"reserved": "cmA"} and exa == 1 and cka == [["1", "cmA"]]
              and code_b == 0 and body_b.get("result") == {"reserved": "cmB"} and exb == 1,
              (code_a, body_a, exa, cka, code_b, body_b, exb))

        # C8b. The DEFAULT arm runs when no arm matches: mode "z" dispatches ONLY the default op
        # (reserve cmD).
        wcd = _wf_id()
        ex0 = _exec_count(base)
        code_d, body_d = run_runner(ccfg, wcd, arguments=json.dumps({"mode": "z"}))
        exd = _exec_count(base) - ex0
        ckd = _mdb(f"SELECT seq, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wcd}') ORDER BY seq")
        check("graph_case_default_when_no_match",
              code_d == 0 and body_d.get("result") == {"reserved": "cmD"} and exd == 1 and ckd == [["1", "cmD"]],
              (code_d, body_d, exd, ckd))

        # C8c. NO durable record at the case boundary: the NCase is PURE (replayed, never persisted).
        # A 1-op straight-line plan and this 1-op-on-taken-path case produce the SAME event count.
        slc = plan_cfg([{"operation": "reserve", "input": {"reservation": "csl"}}])
        wcsl = _wf_id()
        run_runner(slc, wcsl, arguments="{}")
        nev_case = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wca}')")
        nev_sl = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wcsl}')")
        check("graph_case_no_durable_event_at_boundary",
              nev_case == nev_sl and nev_case != [["0"]], (nev_case, nev_sl))

        # C8d. A CLAIMABLE RESUME replays the SAME case selection from the durable args. Submit
        # mode "a" with op A PENDING: arm "a" is chosen, op A requested-not-settled, workflow defers.
        # Nudge claimable + RESUME (no --arguments): it re-derives arm "a" from the durable args
        # (scrutinee arg "mode") and reconciles op A. Had it used {}/CLI, the scrutinee would be
        # MISSING and replay would FAULT -> deferred, not pending.
        pcfg = graph_cfg(case_graph(fault_on_a={"respond_pending": True}), argument_type=CASE_AT)
        wcr = _wf_id()
        opq = (f"SELECT operation_name, JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation')) "
               f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wcr}') ORDER BY operation_seq")
        code_s, body_s = run_runner(pcfg, wcr, arguments=json.dumps({"mode": "a"}))
        op_s = _mdb(opq)
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' WHERE workflow_id = UNHEX('{wcr}')")
        code_r, body_r = run_runner(pcfg, wcr)
        op_r = _mdb(opq)
        check("graph_case_resume_replays_selection_from_durable_args",
              code_s == 9 and body_s.get("workflow") == "pending" and op_s == [["reserve", "cmA"]]
              and code_r == 9 and body_r.get("workflow") == "pending" and op_r == [["reserve", "cmA"]],
              (code_s, body_s, op_s, code_r, body_r, op_r))

        # C8e. An op-UNBALANCED case (arm "a" 2 ops, arm "b"/default 1) is NON-EXECUTABLE: rejected
        # at BUILD (op_depth), surfaced as invalid_config, with NO operation dispatched.
        unbal_case = {"entry": "n0", "nodes": [
            {"kind": "case", "id": "n0", "scrutinee": {"arg": ["mode"]},
             "arms": [{"match": "a", "target": "n1"}, {"match": "b", "target": "n2"}], "default": "n3"},
            {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "u1"}}, "next": "n5"},
            {"kind": "operation", "id": "n5", "operation": "reserve", "input": {"const": {"reservation": "u2"}}, "next": "n4"},
            {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "u3"}}, "next": "n4"},
            {"kind": "operation", "id": "n3", "operation": "reserve", "input": {"const": {"reservation": "u4"}}, "next": "n4"},
            {"kind": "return", "id": "n4", "value": {"const": None}},
        ]}
        wcu = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(graph_cfg(unbal_case, argument_type=CASE_AT), wcu, arguments=json.dumps({"mode": "a"}))
        check("graph_case_unbalanced_rejected_before_dispatch",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C8f. BRANCH REVERSAL compensates ONLY the taken case path: a 2-deep case (the arm op is
        # compensable, a shared final op fails). mode "a" -> reserve cmA settles + checkpoints, the
        # final op is rejected -> reversal compensates ONLY cmA (release). The untaken arms (cmB,
        # cmD) have no operation row, checkpoint, or participant execution.
        def rev_case_graph():
            return {"entry": "n0", "nodes": [
                {"kind": "case", "id": "n0", "scrutinee": {"arg": ["mode"]},
                 "arms": [{"match": "a", "target": "n1"}, {"match": "b", "target": "n2"}], "default": "n3"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "cmA"}}, "next": "n5"},
                {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "cmB"}}, "next": "n5"},
                {"kind": "operation", "id": "n3", "operation": "reserve", "input": {"const": {"reservation": "cmD"}}, "next": "n5"},
                {"kind": "operation", "id": "n5", "operation": "reserve", "input": {"const": {"reservation": "cfin", "_fault": {"reject": True}}}, "next": "n6"},
                {"kind": "return", "id": "n6", "value": {"const": None}},
            ]}
        wcrv = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(graph_cfg(rev_case_graph(), argument_type=CASE_AT), wcrv, arguments=json.dumps({"mode": "a"}))
        ex_d = _exec_count(base) - ex0
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wcrv}')")
        cks = _mdb(f"SELECT seq, reversal_state, JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reservation')) "
                   f"FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wcrv}') ORDER BY seq")
        untaken = _mdb(f"SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id = UNHEX('{wcrv}') "
                       f"AND (input_json LIKE '%cmB%' OR input_json LIKE '%cmD%')")
        check("graph_case_reversal_compensates_taken_path_only",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 2
              and wf == [["5", "2"]] and cks == [["1", "2", "cmA"]] and untaken == [["0"]],
              (code, body, ex_d, wf, cks, untaken))

        # ===== C9. MANUAL-IR CROSS-BRANCH RESULT MERGE (NMerge phi) =====
        # An if rejoins via a `merge` node that binds a local to the TAKEN branch's op result; the
        # merged value then feeds a SHARED downstream op (`confirm`, whose input is a reserve RESULT
        # {"reserved": id}). The merge is pure (replayed, never persisted) and recomputed on resume
        # from the durable branch result — so a branch-local value crosses the join without storage.
        MERGE_AT = {"type": "object", "fields": [{"name": "flag", "type": {"type": "bool"}}]}
        MERGE_SRCS = [{"from": "n1", "value": {"result": {"node": "n1"}}},
                      {"from": "n2", "value": {"result": {"node": "n2"}}}]

        def merge_graph(tail):
            # if flag then reserve mgA else reserve mgB -> merge picked = result(taken branch) ->
            # confirm(picked) -> <tail nodes>. `tail` supplies n5.. and the return.
            nodes = [
                {"kind": "if", "id": "n0", "cond": {"arg": ["flag"]}, "then": "n1", "else": "n2"},
                {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "mgA"}}, "next": "n3"},
                {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "mgB"}}, "next": "n3"},
                {"kind": "merge", "id": "n3", "name": "picked", "sources": MERGE_SRCS, "next": "n4"},
                {"kind": "operation", "id": "n4", "operation": "confirm", "input": {"local": {"name": "picked"}}, "next": "n5"},
            ] + tail
            return {"entry": "n0", "nodes": nodes}

        # C9a. The branch op RESULT is merged into the shared confirm op's input: mgA (true) / mgB
        # (false). confirm dispatches once with the merged reserve result and completes.
        mcfg = graph_cfg(merge_graph([{"kind": "return", "id": "n5", "value": {"result": {"node": "n4"}}}]), argument_type=MERGE_AT)
        wma = _wf_id()
        ex0 = _exec_count(base)
        code_a, body_a = run_runner(mcfg, wma, arguments=json.dumps({"flag": True}))
        exa = _exec_count(base) - ex0
        opa = _mdb(f"SELECT operation_seq, operation_name, "
                   f"COALESCE(JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reserved')), JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation'))) "
                   f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wma}') ORDER BY operation_seq")
        wmb = _wf_id()
        code_b, body_b = run_runner(mcfg, wmb, arguments=json.dumps({"flag": False}))
        # seq1 reserve mgA (its reservation), seq2 confirm whose input is the MERGED reserve result
        # {"reserved":"mgA"} -> the branch op result crossed the join into the shared op.
        check("graph_merge_branch_result_into_shared_op",
              code_a == 0 and body_a.get("result") == {"confirmed": "mgA"} and exa == 2
              and opa == [["1", "reserve", "mgA"], ["2", "confirm", "mgA"]]
              and code_b == 0 and body_b.get("result") == {"confirmed": "mgB"},
              (code_a, body_a, exa, opa, code_b, body_b))

        # C9b. NO durable event at the merge boundary: the if + merge are PURE. The merge graph and
        # an equivalent 2-op straight-line plan (reserve then confirm) produce the SAME event count.
        mpl = plan_cfg([{"operation": "reserve", "input": {"reservation": "mgA"}},
                        {"operation": "confirm", "input": {"reserved": "mgA"}}])
        wmpl = _wf_id()
        run_runner(mpl, wmpl, arguments="{}")
        nev_merge = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wma}')")
        nev_pl = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wmpl}')")
        check("graph_merge_no_durable_event_at_boundary",
              nev_merge == nev_pl and nev_merge != [["0"]], (nev_merge, nev_pl))

        # C9c. RESUME recomputes the merged value from durable RESULTS. The branch op + confirm
        # settle; a trailing op is PENDING, so the workflow defers. A claimable RESUME recomputes the
        # merge from the durable branch result — re-deriving confirm's input (the merged reserve
        # result) identically (confirm stays settled, not re-dispatched) — and reconciles the pending
        # op GET-first (no second PUT). A non-deterministic merge would change confirm's recorded
        # input; this asserts it does not.
        rmcfg = graph_cfg(merge_graph([
            {"kind": "operation", "id": "n5", "operation": "reserve", "input": {"const": {"reservation": "mgq", "_fault": {"respond_pending": True}}}, "next": "n6"},
            {"kind": "return", "id": "n6", "value": {"result": {"node": "n5"}}},
        ]), argument_type=MERGE_AT)
        wmr = _wf_id()
        opq = (f"SELECT operation_seq, operation_name, status, "
               f"COALESCE(JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reserved')), JSON_UNQUOTE(JSON_EXTRACT(input_json,'$.reservation'))) "
               f"FROM tb_mf_operation WHERE workflow_id = UNHEX('{wmr}') ORDER BY operation_seq")
        code_s, body_s = run_runner(rmcfg, wmr, arguments=json.dumps({"flag": True}))
        op_s = _mdb(opq)
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' WHERE workflow_id = UNHEX('{wmr}')")
        pu0, rq0 = _put_count(base), _request_count(base)
        code_r, body_r = run_runner(rmcfg, wmr)
        pu_d, rq_d = _put_count(base) - pu0, _request_count(base) - rq0
        op_r = _mdb(opq)
        check("graph_merge_resume_recomputes_merged_value_identically",
              code_s == 9 and body_s.get("workflow") == "pending"
              and op_s == [["1", "reserve", "2", "mgA"], ["2", "confirm", "2", "mgA"], ["3", "reserve", "1", "mgq"]]
              and code_r == 9 and body_r.get("workflow") == "pending" and op_r == op_s
              and pu_d == 0 and rq_d >= 1,
              (code_s, body_s, op_s, code_r, body_r, op_r, f"put+{pu_d} req+{rq_d}"))

        # C9d. REVERSAL compensates ONLY the taken branch PLUS the shared downstream checkpoint. The
        # branch reserve + the shared confirm settle (2 checkpoints), then a final op is rejected ->
        # reversal unwinds highest-seq first: unconfirm (confirm) then release (reserve mgA) ->
        # reversed. The untaken branch (mgB) has no op row/checkpoint/execution.
        dmcfg = graph_cfg(merge_graph([
            {"kind": "operation", "id": "n5", "operation": "reserve", "input": {"const": {"reservation": "mgfin", "_fault": {"reject": True}}}, "next": "n6"},
            {"kind": "return", "id": "n6", "value": {"const": None}},
        ]), argument_type=MERGE_AT)
        wmd = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(dmcfg, wmd, arguments=json.dumps({"flag": True}))
        ex_d = _exec_count(base) - ex0
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wmd}')")
        cks = _mdb(f"SELECT seq, reversal_state, reverse_operation_name FROM tb_mf_workflow_checkpoint "
                   f"WHERE workflow_id = UNHEX('{wmd}') ORDER BY seq")
        untaken = _mdb(f"SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id = UNHEX('{wmd}') AND input_json LIKE '%mgB%'")
        check("graph_merge_reversal_compensates_taken_branch_and_shared",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 4
              and wf == [["5", "2"]]
              and cks == [["1", "2", "release"], ["2", "2", "unconfirm"]] and untaken == [["0"]],
              (code, body, ex_d, wf, cks, untaken))

        # ===== C10. TYPED EXPRESSION VALIDATION (operation contracts + graph type checking) =====
        # Operations declare input/result TYPES (per-test, via typed_graph_cfg); the runner
        # type-checks the graph at registry build (EArg/EResult/ELocal path resolution, NIf Bool,
        # NMerge agreement, op input type) and folds the types into content_hash. A type error is
        # invalid_config (submission) / revision_unavailable (resume) — never a dispatch.
        T_STR = {"type": "string"}
        T_INT = {"type": "int"}
        RESV_IN = {"type": "object", "fields": [{"name": "reservation", "type": T_STR}]}
        RESV_OUT = {"type": "object", "fields": [{"name": "reserved", "type": T_STR}]}
        SUM_OUT = {"type": "object", "fields": [{"name": "sum", "type": T_INT}]}
        TY_AT = {"type": "object", "fields": [{"name": "flag", "type": {"type": "bool"}}]}
        TY_AT_INT = {"type": "object", "fields": [{"name": "flag", "type": {"type": "int"}}]}

        # C10a. A VALID typed graph still executes: reserve declares input {reservation:string};
        # the branch inputs match, and arg.flag is Bool, so it type-checks and runs.
        tg_ok = {"entry": "n0", "nodes": [
            {"kind": "if", "id": "n0", "cond": {"arg": ["flag"]}, "then": "n1", "else": "n2"},
            {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "tyA"}}, "next": "n3"},
            {"kind": "operation", "id": "n2", "operation": "reserve", "input": {"const": {"reservation": "tyB"}}, "next": "n3"},
            {"kind": "return", "id": "n3", "value": {"const": None}},
        ]}
        tycfg = typed_graph_cfg(tg_ok, {"reserve": {"input_type": RESV_IN, "result_type": RESV_OUT}}, argument_type=TY_AT)
        wty = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(tycfg, wty, arguments=json.dumps({"flag": True}))
        check("typed_valid_graph_executes",
              code == 0 and body.get("workflow") == "completed" and _exec_count(base) - ex0 == 1, (code, body))

        # C10b. INVALID branch condition: arg.flag declared Int -> NIf condition is not Bool ->
        # rejected at build (invalid_config), with NO operation dispatched.
        wbc = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(typed_graph_cfg(tg_ok, {"reserve": {"input_type": RESV_IN}}, argument_type=TY_AT_INT),
                                wbc, arguments=json.dumps({"flag": 1}))
        check("typed_invalid_branch_condition_rejected",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C10c. INVALID op input: reserve declares input {reservation:string}; a const {reservation:5}
        # (int) fails the contract -> rejected at build, no dispatch.
        tg_badin = {"entry": "n0", "nodes": [
            {"kind": "operation", "id": "n0", "operation": "reserve", "input": {"const": {"reservation": 5}}, "next": "n1"},
            {"kind": "return", "id": "n1", "value": {"const": None}},
        ]}
        wbi = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(typed_graph_cfg(tg_badin, {"reserve": {"input_type": RESV_IN}}, argument_type=TY_AT),
                                wbi, arguments=json.dumps({"flag": True}))
        check("typed_invalid_op_input_rejected",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C10d. INVALID merge: branch A's result is {reserved:string}, branch B's is {sum:int}; the
        # merge sources disagree on type -> rejected at build, no dispatch.
        tg_mm = {"entry": "n0", "nodes": [
            {"kind": "if", "id": "n0", "cond": {"arg": ["flag"]}, "then": "n1", "else": "n2"},
            {"kind": "operation", "id": "n1", "operation": "reserve", "input": {"const": {"reservation": "mA"}}, "next": "n3"},
            {"kind": "operation", "id": "n2", "operation": "echo-transform", "input": {"const": {"values": [1]}}, "next": "n3"},
            {"kind": "merge", "id": "n3", "name": "picked",
             "sources": [{"from": "n1", "value": {"result": {"node": "n1"}}}, {"from": "n2", "value": {"result": {"node": "n2"}}}], "next": "n4"},
            {"kind": "return", "id": "n4", "value": {"local": {"name": "picked"}}},
        ]}
        wmm = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(typed_graph_cfg(tg_mm, {"reserve": {"result_type": RESV_OUT}, "echo-transform": {"result_type": SUM_OUT}}, argument_type=TY_AT),
                                wmm, arguments=json.dumps({"flag": True}))
        check("typed_invalid_merge_type_mismatch_rejected",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C10e. RESUME with a CHANGED type contract yields revision_unavailable (never substitution).
        # Submit a pending workflow whose reserve declares result_type {reserved:string} (in the
        # content_hash). Resume under a config where reserve's result_type changed to {reserved:int}:
        # the hash differs from the pin -> revision_unavailable defer, no dispatch/substitution.
        tg_pend = {"entry": "n0", "nodes": [
            {"kind": "operation", "id": "n0", "operation": "reserve", "input": {"const": {"reservation": "tyR", "_fault": {"respond_pending": True}}}, "next": "n1"},
            {"kind": "return", "id": "n1", "value": {"const": None}},
        ]}
        cfg_A = typed_graph_cfg(tg_pend, {"reserve": {"result_type": RESV_OUT}}, argument_type=TY_AT)
        wtr = _wf_id()
        code_s, body_s = run_runner(cfg_A, wtr, arguments=json.dumps({"flag": True}))
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' WHERE workflow_id = UNHEX('{wtr}')")
        cfg_B = typed_graph_cfg(tg_pend, {"reserve": {"result_type": {"type": "object", "fields": [{"name": "reserved", "type": T_INT}]}}}, argument_type=TY_AT)
        ex0 = _exec_count(base)
        code_r, body_r = run_runner(cfg_B, wtr)
        check("typed_resume_changed_contract_revision_unavailable",
              code_s == 9 and body_s.get("workflow") == "pending"
              and code_r == 9 and body_r.get("workflow") == "deferred" and body_r.get("reason") == "revision_unavailable"
              and _exec_count(base) - ex0 == 0,
              (code_s, body_s, code_r, body_r))

        # C10f. COMPENSATION contract: a compensation receives the forward op's checkpoint payload
        # (its input), so the reverse op's declared input type must match the forward op's. reserve
        # declares input {reservation:string} but its compensation `release` declares {reservation:int}
        # -> rejected at build (invalid_config), no dispatch.
        RESV_IN_INT = {"type": "object", "fields": [{"name": "reservation", "type": T_INT}]}
        tg_comp = {"entry": "n0", "nodes": [
            {"kind": "operation", "id": "n0", "operation": "reserve", "input": {"const": {"reservation": "cmp"}}, "next": "n1"},
            {"kind": "return", "id": "n1", "value": {"const": None}},
        ]}
        wcc = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(typed_graph_cfg(tg_comp, {"reserve": {"input_type": RESV_IN}, "release": {"input_type": RESV_IN_INT}}, argument_type=TY_AT),
                                wcc, arguments=json.dumps({"flag": True}))
        check("typed_compensation_input_mismatch_rejected",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "invalid_config"
              and _exec_count(base) - ex0 == 0, (code, body))

        # C10g. A COMPENSATION op's type contract is part of the content_hash: submit a pending
        # workflow whose `release` declares input {reservation:string}; resume where release's input
        # changed to {reservation:int} -> hash differs from the pin -> revision_unavailable.
        ccfg_A = typed_graph_cfg(tg_pend, {"release": {"input_type": RESV_IN}}, argument_type=TY_AT)
        wcr2 = _wf_id()
        code_s, body_s = run_runner(ccfg_A, wcr2, arguments=json.dumps({"flag": True}))
        _mdb(f"UPDATE tb_mf_workflow SET next_attempt_at = '2000-01-01 00:00:00.000000' WHERE workflow_id = UNHEX('{wcr2}')")
        ccfg_B = typed_graph_cfg(tg_pend, {"release": {"input_type": RESV_IN_INT}}, argument_type=TY_AT)
        ex0 = _exec_count(base)
        code_r, body_r = run_runner(ccfg_B, wcr2)
        check("typed_compensation_type_in_content_hash",
              code_s == 9 and body_s.get("workflow") == "pending"
              and code_r == 9 and body_r.get("workflow") == "deferred" and body_r.get("reason") == "revision_unavailable"
              and _exec_count(base) - ex0 == 0,
              (code_s, body_s, code_r, body_r))

        # C5h. EXISTING-workflow reassertion must NOT validate arguments against the ACTIVE
        # type — only a FRESH submission does (a rollout may change the declared type). Create
        # under {a:int}; resubmit under a DIFFERENT active type {b:int} with the SAME (v1-valid)
        # args -> idempotent terminal replay, NOT invalid_arguments.
        ta = {"type": "object", "fields": [{"name": "a", "type": {"type": "int"}}]}
        tb = {"type": "object", "fields": [{"name": "b", "type": {"type": "int"}}]}
        wfre = _wf_id()
        code, body = run_runner(plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=ta),
                                wfre, arguments='{"a":1}')
        check("forward_args_existing_setup", code == 0 and body.get("workflow") == "completed", (code, body))
        code, body = run_runner(plan_cfg([{"operation": "echo-transform", "input": {"values": [1]}}], argument_type=tb),
                                wfre, arguments='{"a":1}')   # {"a":1} is INVALID for {b:int}
        check("forward_args_existing_not_revalidated",
              code == 0 and body.get("workflow") == "already_terminal", (code, body))

        # C5i. CONCURRENT-LEGACY-ID race (pinned via a seeded legacy workflow with no plan pin):
        # a planned RESUME claims it, reloads, finds no durable pin, and RELEASES the lease
        # (never leaks it) before reporting not_found.
        code, body = run_runner(cplan, WF_LEGACY_NO_PIN)   # resume (no --arguments)
        lease = _mdb(f"SELECT lease_owner FROM tb_mf_workflow WHERE workflow_id = UNHEX('{WF_LEGACY_NO_PIN}')")
        check("forward_legacy_id_no_lease_leak",
              code == 2 and body.get("workflow") == "aborted" and body.get("reason") == "not_found"
              and lease == [["NULL"]], (code, body, lease))

        # C6. FIRST-operation rejection begins reversal: a definite forward failure of op1
        # (no prior checkpoint) reverses straight to `reversed` — never blocks. The second
        # step never runs.
        frplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "fr1", "_fault": {"reject": True}}},
                           {"operation": "reserve", "input": {"reservation": "fr2"}}])
        wffr = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(frplan, wffr, arguments="{}")
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wffr}')")
        ncp = _mdb(f"SELECT COUNT(*) FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wffr}')")
        check("forward_first_reject_reverses",
              code == 0 and body.get("workflow") == "reversed"
              and _exec_count(base) - ex0 == 0 and wf == [["5", "2"]] and ncp == [["0"]],
              (code, body, wf, ncp))

        # === SUB-STEP D: forward failure -> automatic reversal. op1 succeeds (checkpoint),
        # op2 is DEFINITELY rejected; the runner BEGINS reversal and unwinds op1 in the
        # SAME drive, reaching reversed. Incl. restart + lost-ack. ===
        # D1. op1 reserve succeeds, op2 reserve is rejected (400) -> begin_reversal ->
        # compensate op1 (release) -> reversed. exec: reserve d1 + release d1 = 2 (op2
        # rejected before Singular, no exec).
        dplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "d1"}},
                          {"operation": "reserve", "input": {"reservation": "d2", "_fault": {"reject": True}}}])
        wfd = _wf_id()
        ex0 = _exec_count(base)
        code, body = run_runner(dplan, wfd, arguments="{}")
        ex_d = _exec_count(base) - ex0
        check("forward_fail_begins_reversal",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 2, (code, body, ex_d))
        # D1b. DURABLE evidence: workflow reversed(5) RETAINING reverse direction(2), op1's
        # checkpoint reversed(2), and the audit trail shows reversal_begun then
        # compensation_settled — the forward failure drove the whole transition.
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{wfd}')")
        cks = _mdb(f"SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id = UNHEX('{wfd}')")
        evs = _mdb(f"SELECT kind FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{wfd}') "
                   f"AND kind IN ('reversal_begun','compensation_settled') ORDER BY event_seq")
        check("forward_fail_reverses_durable",
              wf == [["5", "2"]] and cks == [["2"]]
              and evs == [["reversal_begun"], ["compensation_settled"]], (wf, cks, evs))

        # D2. RESTART across the forward->reversal transition: a worker settled op1 and
        # requested op2 (a reject-faulted op) then crashed BEFORE beginning reversal. The
        # restart re-dispatches op2 from durable state, hits the rejection, begins reversal,
        # and unwinds op1 -> reversed. exec: release f1 = 1 (op2 rejected, op1 seeded).
        frplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "f1"}},
                           {"operation": "reserve", "input": {"reservation": "f2", "_fault": {"reject": True}}}])
        ex0 = _exec_count(base)
        code, body = run_runner(frplan, WF_FWD_FAIL_RESTART)
        ex_d = _exec_count(base) - ex0
        wf = _mdb(f"SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id = UNHEX('{WF_FWD_FAIL_RESTART}')")
        check("forward_fail_restart",
              code == 0 and body.get("workflow") == "reversed" and ex_d == 1
              and wf == [["5", "2"]], (code, body, ex_d, wf))

        # D3. LOST ACK across forward AND compensation: op1's reserve commits then drops
        # its ack (its input also drives the release, which ALSO drops its ack); both
        # reconcile via GET. op2 is rejected -> reversal -> reversed. We assert the
        # invariant that proves the reconciles HAPPENED (not just that exec stayed at 2):
        # effectively-once (exec +2), at least the 3 base dispatches (put >= 3), and
        # reconcile GETs in excess of PUTs (req - put >= 2 — op1 and the release each
        # reconcile). If the delay fault were ignored every dispatch would return 200 on
        # the PUT with NO GETs (req == put), so req - put >= 2 catches it. Exact counts
        # are not pinned: a delayed FORWARD op's reconcile GET may race ahead of commit
        # visibility and take the 404 -> re-PUT path, so the wire shape varies by a PUT/GET
        # while staying effectively-once (the clean-prior reverse-stack lost-ack is exact).
        laplan = plan_cfg([{"operation": "reserve", "input": {"reservation": "la1", "_fault": {"delay_after_commit_ms": 5000}}},
                           {"operation": "reserve", "input": {"reservation": "la2", "_fault": {"reject": True}}}])
        wfla = _wf_id()
        ex0, pu0, rq0 = _exec_count(base), _put_count(base), _request_count(base)
        code, body = run_runner(laplan, wfla, arguments="{}")
        ex_d, pu_d, rq_d = _exec_count(base) - ex0, _put_count(base) - pu0, _request_count(base) - rq0
        check("forward_fail_lost_ack",
              code == 0 and body.get("workflow") == "reversed"
              and ex_d == 2 and pu_d >= 3 and (rq_d - pu_d) >= 2,
              (code, body, f"exec+{ex_d} put+{pu_d} req+{rq_d}"))

        # ===== C11. TEXTUAL FRONTEND (parser slice 1): lowering PARITY =====
        # A `.mf` source lowered through the runner's --lower-source frontend must produce the SAME
        # config the manual IR consumes — proven by IDENTICAL content_hash with a hand-authored
        # equivalent and IDENTICAL execution. The frontend adds NO runtime semantics: it reuses the
        # existing plan/argument_type/contract machinery. A malformed source fails at lowering,
        # before any config exists to run (so no dispatch is even possible). (Placed BEFORE the
        # participant-down teardown below, since the execute parity check dispatches to the stub.)
        P_RESV_IN = {"type": "object", "fields": [{"name": "reservation", "type": {"type": "string"}}]}
        P_RESV_OUT = {"type": "object", "fields": [{"name": "reserved", "type": {"type": "string"}}]}
        P_AT = {"type": "object", "fields": [{"name": "amount", "type": {"type": "int"}},
                                             {"name": "currency", "type": {"type": "string"}}]}
        mf_src = (
            "# straight-line refund (slice 1)\n"
            "args { amount: int, currency: string }\n"
            "op reserve { input: { reservation: string } result: { reserved: string } }\n"
            "op release { input: { reservation: string } }\n"
            "steps {\n"
            "  reserve { \"reservation\": \"ps1\" }\n"
            "  reserve { \"reservation\": \"ps2\" }\n"
            "}\n"
        )
        lr_code, lowered = lower_source(mf_src)
        # Hand-authored manual equivalent: the SAME flat plan + argument_type + the SAME input/result
        # contracts overlaid on reserve/release (routing untouched).
        hand = dict(runner_cfg)
        hops = []
        for o in runner_cfg["operations"]:
            o2 = dict(o)
            if o2["name"] == "reserve":
                o2["input_type"] = P_RESV_IN; o2["result_type"] = P_RESV_OUT
            if o2["name"] == "release":
                o2["input_type"] = P_RESV_IN
            hops.append(o2)
        hand["operations"] = hops
        hand["plan"] = [{"operation": "reserve", "input": {"reservation": "ps1"}},
                        {"operation": "reserve", "input": {"reservation": "ps2"}}]
        hand["argument_type"] = P_AT
        hf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(hand, hf); hf.close()
        plan_cfgs.append(hf.name)
        h_lowered = emit_content_hash(lowered) if lowered else "<none>"
        h_hand = emit_content_hash(hf.name)
        check("parser_lowering_content_hash_parity",
              lr_code == 0 and lowered is not None and h_lowered != "" and h_lowered == h_hand,
              (lr_code, lowered, h_lowered, h_hand))

        # Both the lowered and the hand-authored config EXECUTE to the same outcome (the final op's
        # result {reserved: ps2}, two dispatches each) — lowering parity at runtime, not just identity.
        args_p = json.dumps({"amount": 1, "currency": "USD"})
        wlp = _wf_id(); ex0 = _exec_count(base)
        code_l, body_l = run_runner(lowered, wlp, arguments=args_p)
        exl = _exec_count(base) - ex0
        wlh = _wf_id(); ex0 = _exec_count(base)
        code_h, body_h = run_runner(hf.name, wlh, arguments=args_p)
        exh = _exec_count(base) - ex0
        check("parser_lowered_executes_to_parity_result",
              code_l == 0 and body_l.get("workflow") == "completed" and body_l.get("result") == {"reserved": "ps2"} and exl == 2
              and code_h == 0 and body_h.get("workflow") == "completed" and body_h.get("result") == body_l.get("result") and exh == 2,
              (code_l, body_l, exl, code_h, body_h, exh))

        # A malformed source fails at LOWERING (nonzero, no config emitted) -> before any dispatch.
        ex0 = _exec_count(base)
        bad_code, bad_cfg = lower_source("args { amount: int }\n")   # no 'steps' block
        check("parser_malformed_source_fails_before_dispatch",
              bad_code != 0 and bad_cfg is None and _exec_count(base) - ex0 == 0,
              (bad_code, bad_cfg))

        # A contract for an operation absent from the routing registry is rejected at lowering
        # (a dropped contract would be absent from content_hash) — also before any dispatch.
        ghost_code, ghost_cfg = lower_source(
            "op ghost { input: { x: int } }\nsteps { reserve { \"reservation\": \"g\" } }\n")
        check("parser_unknown_op_contract_rejected",
              ghost_code != 0 and ghost_cfg is None, (ghost_code, ghost_cfg))

        # --lower-source VALIDATES the emitted config through the real build path (DB-free, no
        # dispatch) before printing — so a semantically-invalid source (here: a step op absent from
        # the routing registry, which the parser alone would not catch) fails at lowering, not later.
        step_code, step_cfg = lower_source("steps { ghoststep { \"x\": 1 } }\n")
        check("parser_lower_validates_emitted_config",
              step_code != 0 and step_cfg is None, (step_code, step_cfg))

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
        # Same for a completed MULTI-operation PLAN workflow (wfc from C1): the
        # terminal replay returns the FINAL operation's local result (c2), not the
        # first (c1), with no dependency on the now-down participant.
        code, body = run_runner(cplan, wfc)
        check("terminal_rerun_multiop_final_result", code == 0
              and body.get("workflow") == "already_terminal"
              and body.get("result") == {"reserved": "c2"}, (code, body))
        # Terminal replay is registry-CONFIG-independent (static-review item 3): the same
        # completed plan workflow replays its final result even when the current
        # operations/participants config is MALFORMED (an unknown participant ref that
        # _validate_registry would reject). Terminal replay reads only durable state — it
        # never builds or validates the registry.
        broken = dict(runner_cfg)
        broken["participants"] = []
        broken["operations"] = [{"name": "reserve", "participant": "ghost", "schema_version": 1}]
        _bf2 = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(broken, _bf2); _bf2.close(); plan_cfgs.append(_bf2.name)
        code, body = run_runner(_bf2.name, wfc)
        check("terminal_replay_registry_config_independent", code == 0
              and body.get("workflow") == "already_terminal"
              and body.get("result") == {"reserved": "c2"}, (code, body))

    finally:
        stub.terminate()
        try:
            stub.wait(timeout=5)
        except Exception:
            stub.kill()
        os.unlink(scf.name); os.unlink(rcf.name)
        for p in plan_cfgs:
            os.unlink(p)

    # Display counts are DERIVED (always honest). EXPECTED_CHECKS is a completeness guard,
    # NOT the display denominator: a deleted/bypassed check drifts the ran-count from this
    # manifest and FAILS the run (so N/N can't hide a gap).
    EXPECTED_CHECKS = 106
    total = passed + len(failures)
    if total != EXPECTED_CHECKS:
        failures.append(f"completeness_guard: ran {total} checks, expected {EXPECTED_CHECKS}")
    print(f"coordinator<->singular integration: {passed}/{total} passed (expected {EXPECTED_CHECKS})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
