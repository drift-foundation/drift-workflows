#!/usr/bin/env python3
"""Focused SQL regression for the operation lifecycle stored procedures.

Covers the hardening invariants:
  - NULL / invalid fencing token must SIGNAL (never bypass the fence)
  - request idempotency + command-identity conflict on replay
  - repeated settlement is idempotent (already_settled, immutable result)
  - status/result table invariant
  - happy-path lifecycle ends with workflow completed + checkpoint

Run via the mariachi venv python (has PyMySQL):
  ../../../mariachi/.venv/bin/python db/tests/sp_operation_test.py
Requires the `microflows` schema loaded (`just db-load-schema`) and MDB_ROOT_PWD.
"""
import datetime
import json
import os
import sys
import uuid

import pymysql

HOST = os.environ.get("MDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("MDB_PORT", "34114"))
USER = os.environ.get("MDB_USER", "root")
PWD = os.environ.get("MDB_ROOT_PWD", "rootpw")

# Random per-run workflow id so concurrent gate runs never collide.
WF = os.urandom(16)
EXEC = bytes.fromhex("e9000000000000000000000000000009")
OPID = bytes.fromhex("00000000000000000000000000000b09")
WRONG_OPID = bytes.fromhex("0000000000000000000000000000ffff")
SCRIPT = f"sp-op-test-{uuid.uuid4().hex[:8]}"

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name}: {detail}")


def call(cur, proc, args):
    """Call an SP returning one JSON `result`; return (ok, value_or_error)."""
    try:
        cur.execute(f"CALL {proc}({','.join(['%s'] * len(args))})", args)
        row = cur.fetchone()
        return True, json.loads(row[0]) if row else None
    except pymysql.MySQLError as e:
        return False, e


def main():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD,
                           database="microflows", autocommit=True)
    cur = conn.cursor()
    # clean slate
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (WF,))

    ts = lambda s: f"2026-02-01 12:00:{s:02d}.000000"  # noqa: E731

    _, r = call(cur, "sp_mf_workflow_create",
                (WF, SCRIPT, 1, ts(0), ts(0), '{"pos":"start"}', "{}"))
    check("create", r and r["outcome"] == "created", r)

    _, r = call(cur, "sp_mf_workflow_claim", (SCRIPT, EXEC, ts(1), "2026-02-01 12:30:00.000000"))
    check("claim", r and r["outcome"] == "claimed", r)
    token = r["fencing_token"]

    # NULL fencing token must SIGNAL, never bypass.
    ok, err = call(cur, "sp_mf_operation_request",
                   (WF, EXEC, None, 1, OPID, "echo-transform", 1, '{"values":[1]}', "h1", '{"pos":"d"}', ts(2), "{}"))
    check("null_fencing_signals", (not ok) and "MfFencingTokenInvalid" in str(err), (ok, err))

    # Valid request.
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", 1, '{"values":[1]}', "h1", '{"pos":"d"}', ts(2), "{}"))
    check("request", r and r["outcome"] == "requested", r)

    # Replay with a different input_hash -> operation_conflict.
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", 1, '{"values":[9]}', "hX", '{"pos":"d"}', ts(3), "{}"))
    check("request_conflict", r and r["outcome"] == "operation_conflict", r)

    # Replay with a different schema_version (same id/name/hash) -> operation_conflict
    # (schema_version is part of the immutable request identity).
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", 2, '{"values":[1]}', "h1", '{"pos":"d"}', ts(3), "{}"))
    check("request_schema_version_conflict", r and r["outcome"] == "operation_conflict", r)

    # Matching replay -> exists (authoritative id).
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", 1, '{"values":[1]}', "h1", '{"pos":"d"}', ts(4), "{}"))
    check("request_replay_exists", r and r["outcome"] == "exists" and r["operation_id"] == OPID.hex(), r)

    # Stale token on an EXISTING matching request must FENCE (not 'exists').
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, 999, 1, OPID, "echo-transform", 1, '{"values":[1]}', "h1", '{"pos":"d"}', ts(5), "{}"))
    check("request_existing_stale_token_fence_lost", r and r["outcome"] == "fence_lost", r)

    # Stale token on a NEW operation_seq -> fence_lost.
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, 999, 2, bytes.fromhex("00000000000000000000000000000b0a"),
                 "echo-transform", 1, '{"values":[2]}', "h2", '{"pos":"d"}', ts(6), "{}"))
    check("request_stale_token_fence_lost", r and r["outcome"] == "fence_lost", r)

    # Settle with the WRONG operation_id must conflict (never settle op 1 with
    # another operation's response).
    _, r = call(cur, "sp_mf_operation_settle",
                (WF, EXEC, token, 1, WRONG_OPID, 1, '{"sum":1}', '{"sum":1}', '{"pos":"done"}', ts(7), "{}", 1))
    check("settle_wrong_opid_conflict", r and r["outcome"] == "operation_conflict", r)

    # Settle (correct id, final), then repeated settle is idempotent.
    _, r = call(cur, "sp_mf_operation_settle",
                (WF, EXEC, token, 1, OPID, 1, '{"sum":1}', '{"sum":1}', '{"pos":"done"}', ts(8), "{}", 1))
    check("settle", r and r["outcome"] == "settled" and r["result"] == {"sum": 1}, r)

    _, r = call(cur, "sp_mf_operation_settle",
                (WF, EXEC, token, 1, OPID, 1, '{"sum":1}', '{"sum":1}', '{"pos":"done"}', ts(9), "{}", 1))
    check("settle_idempotent", r and r["outcome"] == "already_settled" and r["result"] == {"sum": 1}, r)

    # Final invariants: workflow completed (state 4, disp 1, lease cleared), op succeeded, checkpoint present.
    cur.execute("SELECT state, current_disposition, lease_owner FROM tb_mf_workflow WHERE workflow_id=%s", (WF,))
    state, disp, owner = cur.fetchone()
    check("workflow_completed", state == 4 and disp == 1 and owner is None, (state, disp, owner))
    cur.execute("SELECT status, result_json FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (WF,))
    st, res = cur.fetchone()
    check("operation_succeeded", st == 2 and json.loads(res) == {"sum": 1}, (st, res))
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s", (WF,))
    check("checkpoint_created", cur.fetchone()[0] == 1)

    # claim_by_id / inspect on the now-completed workflow.
    absent = os.urandom(16)
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (WF, EXEC, ts(10), "2026-02-01 12:30:00.000000"))
    check("claim_by_id_terminal_not_claimable", r and r["outcome"] == "not_claimable", r)
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (absent, EXEC, ts(10), "2026-02-01 12:30:00.000000"))
    check("claim_by_id_absent_not_found", r and r["outcome"] == "not_found", r)
    _, r = call(cur, "sp_mf_workflow_inspect", (WF, ts(10)))
    check("inspect_terminal", r and r["outcome"] == "found" and r["state"] == 4
          and r["is_terminal"] == 1 and r["leased"] == 0, r)
    _, r = call(cur, "sp_mf_workflow_inspect", (absent, ts(10)))
    check("inspect_absent_not_found", r and r["outcome"] == "not_found", r)

    # claim_by_id claims a fresh, due workflow and inspect then reports it leased.
    wf2 = os.urandom(16)
    call(cur, "sp_mf_workflow_create", (wf2, SCRIPT, 1, ts(0), ts(0), '{"pos":"start"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf2, EXEC, ts(1), "2026-02-01 12:30:00.000000"))
    check("claim_by_id_claims_due", r and r["outcome"] == "claimed"
          and r["workflow_id"] == wf2.hex(), r)
    _, r = call(cur, "sp_mf_workflow_inspect", (wf2, ts(1)))
    check("inspect_active_leased", r and r["outcome"] == "found"
          and r["is_terminal"] == 0 and r["leased"] == 1, r)
    for t in ("tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf2,))

    # operation_result on the completed main workflow (local authoritative result).
    _, r = call(cur, "sp_mf_operation_result", (WF, 1))
    check("operation_result_succeeded", r and r["outcome"] == "succeeded" and r["result"] == {"sum": 1}, r)
    _, r = call(cur, "sp_mf_operation_result", (WF, 99))
    check("operation_result_not_found", r and r["outcome"] == "not_found", r)

    # Fresh workflow wf3: a requested (unsettled) op, exact skew defer_until, and
    # release fence-loss.
    wf3 = os.urandom(16)
    T = lambda s: f"2026-02-01 13:00:{s:02d}.000000"  # noqa: E731
    OPID3 = bytes.fromhex("00000000000000000000000000000c33")
    call(cur, "sp_mf_workflow_create", (wf3, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf3, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok3 = r["fencing_token"]
    _, r = call(cur, "sp_mf_operation_request",
                (wf3, EXEC, tok3, 1, OPID3, "echo-transform", 1, '{"values":[2]}', "h3", '{"pos":"d"}', T(2), "{}"))
    check("wf3_request", r and r["outcome"] == "requested", r)
    _, r = call(cur, "sp_mf_operation_result", (wf3, 1))
    check("operation_result_requested", r and r["outcome"] == "requested", r)

    # current_event_ts is now T(2); a non-increasing event_ts defers to T(2)+5s.
    _, r = call(cur, "sp_mf_operation_request",
                (wf3, EXEC, tok3, 2, bytes.fromhex("00000000000000000000000000000c34"),
                 "echo-transform", 1, '{"values":[3]}', "h4", '{"pos":"d"}', T(2), "{}"))
    check("request_skew_defer_until", r and r["outcome"] == "event_time_skew"
          and r["defer_until"] == "2026-02-01 13:00:07.000000", r)
    _, r = call(cur, "sp_mf_operation_settle",
                (wf3, EXEC, tok3, 1, OPID3, 1, '{"sum":2}', '{"sum":2}', '{"pos":"done"}', T(2), "{}", 1))
    check("settle_skew_defer_until", r and r["outcome"] == "event_time_skew"
          and r["defer_until"] == "2026-02-01 13:00:07.000000", r)

    # release with a wrong token -> fence_lost (the runner maps this to a distinct
    # defer_failed, never reporting a committed defer).
    _, r = call(cur, "sp_mf_workflow_release", (wf3, EXEC, 999, T(3), T(4)))
    check("release_fence_lost", r and r["outcome"] == "fence_lost", r)
    for t in ("tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf3,))

    # --- multi-operation plan: INTERMEDIATE vs FINAL settle (sub-step C). is_final=0
    # records the result + checkpoint but stays forward(1) RETAINING the lease, so the
    # same drive proceeds to the next operation (and a crash leaves it claimable from
    # the durable operation/checkpoint state); is_final=1 on the last op completes.
    wf5 = os.urandom(16)
    op5a = bytes.fromhex("00000000000000000000000000000e51")
    op5b = bytes.fromhex("00000000000000000000000000000e52")
    plan_h = bytes.fromhex("a1" * 16)
    for t in ("tb_mf_workflow_plan", "tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf5,))
    # CREATE + PIN the 2-step plan ATOMICALLY. Re-creating the SAME identity is
    # idempotent (exists); a DIFFERENT plan hash is plan_conflict (a config change can't
    # silently adopt a new plan under an existing workflow).
    _, r = call(cur, "sp_mf_workflow_create_planned", (wf5, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}", plan_h, 2))
    check("create_planned", r and r["outcome"] == "created", r)
    _, r = call(cur, "sp_mf_workflow_create_planned", (wf5, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}", plan_h, 2))
    check("create_planned_idempotent", r and r["outcome"] == "exists", r)
    _, r = call(cur, "sp_mf_workflow_create_planned", (wf5, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}", bytes.fromhex("b2" * 16), 2))
    check("create_planned_conflict", r and r["outcome"] == "plan_conflict" and r["plan_length"] == 2, r)
    # The immutable SCRIPT identity also participates: same plan, different revision is
    # a plan_conflict, not exists.
    _, r = call(cur, "sp_mf_workflow_create_planned", (wf5, SCRIPT, 2, T(0), T(0), '{"pos":"start"}', "{}", plan_h, 2))
    check("create_planned_script_conflict", r and r["outcome"] == "plan_conflict", r)
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf5, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok5 = r["fencing_token"]
    # Request op2 BEFORE op1 is settled: the durable request ordering rejects it
    # (predecessor incomplete) — the remote side effect cannot occur out of order.
    _, r = call(cur, "sp_mf_operation_request",
                (wf5, EXEC, tok5, 2, op5b, "reserve", 1, '{"reservation":"m2"}', "h2", '{"pos":"d"}', T(2), "{}"))
    check("request_predecessor_incomplete", r and r["outcome"] == "plan_violation"
          and r["reason"] == "predecessor_incomplete", r)
    # A seq beyond the plan is rejected at REQUEST too (before any side effect).
    _, r = call(cur, "sp_mf_operation_request",
                (wf5, EXEC, tok5, 3, op5b, "reserve", 1, '{"reservation":"m3"}', "h3", '{"pos":"d"}', T(2), "{}"))
    check("request_seq_out_of_range", r and r["outcome"] == "plan_violation"
          and r["reason"] == "seq_out_of_range", r)
    # op1: request, then a settle claiming FINALITY (is_final=1) is REJECTED — the
    # pinned plan_length=2 makes seq 1 NOT final; a runner defect can't complete early.
    call(cur, "sp_mf_operation_request",
         (wf5, EXEC, tok5, 1, op5a, "reserve", 1, '{"reservation":"m1"}', "h1", '{"pos":"op:1:dispatched"}', T(2), "{}"))
    _, r = call(cur, "sp_mf_operation_settle",
                (wf5, EXEC, tok5, 1, op5a, 1, '{"reserved":"m1"}', '{"reservation":"m1"}', '{"pos":"x"}', T(3), "{}", 1))
    check("settle_finality_violation", r and r["outcome"] == "plan_violation" and r["reason"] == "finality", r)
    # A seq OUTSIDE the pinned plan (seq 3 of a 2-step plan) is rejected.
    _, r = call(cur, "sp_mf_operation_settle",
                (wf5, EXEC, tok5, 3, op5a, 3, '{"reserved":"m1"}', '{"reservation":"m1"}', '{"pos":"x"}', T(3), "{}", 0))
    check("settle_seq_out_of_range", r and r["outcome"] == "plan_violation" and r["reason"] == "seq_out_of_range", r)
    # A checkpoint_seq that does not map to the operation_seq is rejected.
    _, r = call(cur, "sp_mf_operation_settle",
                (wf5, EXEC, tok5, 1, op5a, 2, '{"reserved":"m1"}', '{"reservation":"m1"}', '{"pos":"x"}', T(3), "{}", 0))
    check("settle_checkpoint_mismatch", r and r["outcome"] == "plan_violation" and r["reason"] == "checkpoint_mismatch", r)
    # The correct INTERMEDIATE settle (is_final=0, checkpoint_seq=seq) for seq 1 succeeds.
    _, r = call(cur, "sp_mf_operation_settle",
                (wf5, EXEC, tok5, 1, op5a, 1, '{"reserved":"m1"}', '{"reservation":"m1"}', '{"pos":"op:1:settled"}', T(3), "{}", 0))
    check("settle_intermediate", r and r["outcome"] == "settled", r)
    cur.execute("SELECT state, current_disposition, lease_owner, fencing_token FROM tb_mf_workflow WHERE workflow_id=%s", (wf5,))
    st, disp, owner, ftok = cur.fetchone()
    check("intermediate_stays_forward_lease_retained",
          st == 1 and disp == 0 and owner == EXEC and ftok == tok5, (st, disp, owner, ftok))
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s AND kind='operation_settled'", (wf5,))
    check("intermediate_emits_operation_settled", cur.fetchone()[0] == 1)
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s AND kind='workflow_completed'", (wf5,))
    check("intermediate_not_completed", cur.fetchone()[0] == 0)
    # op2 under the SAME retained token (proves still forward + leased), FINAL settle.
    _, r = call(cur, "sp_mf_operation_request",
                (wf5, EXEC, tok5, 2, op5b, "reserve", 1, '{"reservation":"m2"}', "h2", '{"pos":"op:2:dispatched"}', T(4), "{}"))
    check("intermediate_next_request_same_token", r and r["outcome"] == "requested", r)
    _, r = call(cur, "sp_mf_operation_settle",
                (wf5, EXEC, tok5, 2, op5b, 2, '{"reserved":"m2"}', '{"reservation":"m2"}', '{"pos":"complete"}', T(5), "{}", 1))
    check("settle_final", r and r["outcome"] == "settled", r)
    cur.execute("SELECT state, current_disposition, lease_owner FROM tb_mf_workflow WHERE workflow_id=%s", (wf5,))
    st, disp, owner = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s", (wf5,))
    ncp = cur.fetchone()[0]
    check("final_completes_with_stack", st == 4 and disp == 1 and owner is None and ncp == 2, (st, disp, owner, ncp))
    for t in ("tb_mf_workflow_plan", "tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf5,))

    # --- durable OPERATIONAL dispatch deferral (sp_mf_operation_dispatch_defer):
    # a repairable config state (e.g. pinned binding unavailable). Forward stays
    # forward, operation status stays requested, continuation preserved, lease
    # cleared; the 'operation_dispatch_deferred' audit event is appended ONCE per
    # reason (deduped on retry). NOT a failure, NOT blocked_resolution.
    wf4 = os.urandom(16)
    op4 = bytes.fromhex("00000000000000000000000000000d41")
    for t in ("tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf4,))
    call(cur, "sp_mf_workflow_create", (wf4, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf4, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok4 = r["fencing_token"]
    call(cur, "sp_mf_operation_request",
         (wf4, EXEC, tok4, 1, op4, "echo-transform", 1, '{"values":[1]}', "h1", '{"pos":"d"}', T(2), "{}"))
    _, r = call(cur, "sp_mf_operation_dispatch_defer",
                (wf4, EXEC, tok4, T(3), T(8), T(3), "pinned_contract_unavailable"))
    check("dispatch_defer", r and r["outcome"] == "deferred", r)
    # Re-claim and defer the SAME reason: deduped — no second audit event.
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf4, EXEC, T(9), "2026-02-01 13:30:00.000000"))
    call(cur, "sp_mf_operation_dispatch_defer",
         (wf4, EXEC, r["fencing_token"], T(10), T(15), T(10), "pinned_contract_unavailable"))
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s "
                "AND kind='operation_dispatch_deferred'", (wf4,))
    n_def = cur.fetchone()[0]
    check("dispatch_defer_dedup", n_def == 1, f"deferred events={n_def} (expected 1)")
    cur.execute("SELECT w.state, w.lease_owner, o.status FROM tb_mf_workflow w "
                "JOIN tb_mf_operation o USING (workflow_id) WHERE w.workflow_id=%s", (wf4,))
    st, lo, op_st = cur.fetchone()
    check("dispatch_defer_forward_unchanged", st == 1 and lo is None and op_st == 1, (st, lo, op_st))
    # A DIFFERENT reason is NOT deduped: it appends a second event carrying the new
    # reason in the payload.
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf4, EXEC, T(16), "2026-02-01 13:30:00.000000"))
    call(cur, "sp_mf_operation_dispatch_defer",
         (wf4, EXEC, r["fencing_token"], T(17), T(22), T(17), "operation_request_absent"))
    cur.execute("SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.reason')) FROM tb_mf_workflow_event "
                "WHERE workflow_id=%s AND kind='operation_dispatch_deferred' ORDER BY event_seq", (wf4,))
    reasons = [row[0] for row in cur.fetchall()]
    check("dispatch_defer_reason_change",
          reasons == ["pinned_contract_unavailable", "operation_request_absent"], reasons)
    # next_attempt_at must be strictly future, or a malformed caller spins a hot loop.
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf4, EXEC, T(23), "2026-02-01 13:30:00.000000"))
    _, err = call(cur, "sp_mf_operation_dispatch_defer",
                  (wf4, EXEC, r["fencing_token"], T(24), T(24), T(24), "pinned_contract_unavailable"))
    check("dispatch_defer_requires_future", (not _) and "MfNextAttemptNotFuture" in str(err), (_, err))
    # Stale token must fence, never claim a committed defer.
    _, r = call(cur, "sp_mf_operation_dispatch_defer",
                (wf4, EXEC, 999, T(25), T(26), T(25), "pinned_contract_unavailable"))
    check("dispatch_defer_fence_lost", r and r["outcome"] == "fence_lost", r)
    for t in ("tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf4,))

    # --- reversal transitions (sub-step A): the TRANSITION LAYER enforces its own
    # preconditions (durable failed op, reverse order, fence, time discipline,
    # lease-independent replay). Checkpoints + the failed forward op are inserted
    # directly so reversal correctness is isolated from the forward path.
    def _seed_checkpoint(wf, seq, op_id, payload):
        cur.execute(
            "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, "
            "payload, reversal_state, created_at, updated_at) VALUES (%s,%s,'reserve',%s,%s,1,%s,%s)",
            (wf, seq, op_id, payload, T(2), T(2)))

    def _seed_op(wf, op_seq, op_id, status, result):
        cur.execute(
            "INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
            "schema_version, input_json, input_hash, status, result_json, created_at, updated_at) "
            "VALUES (%s,%s,%s,'reserve',1,'{}','h',%s,%s,%s,%s)",
            (wf, op_seq, op_id, status, result, T(2), T(2)))

    # reverse_request with the durable compensation binding (pinned reverse contract
    # + input identity) — 'release' is the compensation for the 'reserve' forward op.
    def rev_req(wf, tok, seq, rid, ets):
        return call(cur, "sp_mf_checkpoint_reverse_request",
                    (wf, EXEC, tok, seq, rid, "release", 1, '{"undo":true}', "rh", ets))

    wf5 = os.urandom(16)
    cp1_op = bytes.fromhex("00000000000000000000000000000e51")
    cp2_op = bytes.fromhex("00000000000000000000000000000e52")
    failed5 = bytes.fromhex("00000000000000000000000000000e53")
    rev1_id = bytes.fromhex("00000000000000000000000000000e5a")
    rev2_id = bytes.fromhex("00000000000000000000000000000e5b")
    call(cur, "sp_mf_workflow_create", (wf5, SCRIPT, 1, T(0), T(0), '{"pos":"op:3:dispatched"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf5, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok5 = r["fencing_token"]
    _seed_checkpoint(wf5, 1, cp1_op, '{"reservation":"r1"}')
    _seed_checkpoint(wf5, 2, cp2_op, '{"reservation":"r2"}')
    _seed_op(wf5, 3, failed5, 1, None)  # the durably-requested op that was rejected
    # begin_reversal verifies the durable failed op + opens at the TOP checkpoint
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf5, EXEC, tok5, 3, failed5, T(3), "forward_op_rejected"))
    check("begin_reversal_reversing", r and r["outcome"] == "reversing" and r["top_seq"] == 2, r)
    cur.execute("SELECT state, execution_direction, current_disposition FROM tb_mf_workflow WHERE workflow_id=%s", (wf5,))
    check("begin_reversal_state", cur.fetchone() == (2, 2, 2), "expected reversing/reverse/failed")
    # replay BOUND to the durable trigger: the SAME op replays as already_reversing;
    # a DIFFERENT op must not masquerade as the same begin-reversal command.
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf5, EXEC, tok5, 3, failed5, T(3), "forward_op_rejected"))
    check("begin_reversal_replay_same_trigger",
          r and r["outcome"] == "already_begun" and r["state"] == 2, r)
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf5, EXEC, tok5, 1, os.urandom(16), T(3), "x"))
    check("begin_reversal_trigger_mismatch", r and r["outcome"] == "trigger_mismatch", r)
    # reverse_head is AUTHORITATIVE (not the continuation): top active checkpoint,
    # not yet dispatched -> pending with the forward identity to derive the binding.
    _, r = call(cur, "sp_mf_checkpoint_reverse_head", (wf5,))
    check("reverse_head_pending",
          r and r["outcome"] == "pending" and r["seq"] == 2 and r["operation_name"] == "reserve"
          and r["payload"] == {"reservation": "r2"}, r)
    # reverse ORDER enforced: cannot compensate seq 1 while seq 2 is the top active
    _, r = rev_req(wf5, tok5, 1, rev1_id, T(4))
    check("reverse_request_out_of_order", r and r["outcome"] == "out_of_order" and r["top_seq"] == 2, r)
    # time discipline: a non-advancing event_ts is rejected
    _, r = rev_req(wf5, tok5, 2, rev2_id, T(3))
    check("reverse_request_skew", r and r["outcome"] == "event_time_skew", r)
    # compensate the TOP checkpoint (seq 2) first — persists the durable binding
    _, r = rev_req(wf5, tok5, 2, rev2_id, T(4))
    check("reverse_request_requested",
          r and r["outcome"] == "requested" and r["reverse_invocation_id"] == rev2_id.hex(), r)
    # reverse_head now reports the DISPATCHED binding (pinned reverse contract)
    _, r = call(cur, "sp_mf_checkpoint_reverse_head", (wf5,))
    check("reverse_head_dispatched",
          r and r["outcome"] == "dispatched" and r["seq"] == 2
          and r["reverse_invocation_id"] == rev2_id.hex()
          and r["reverse_operation_name"] == "release" and r["reverse_schema_version"] == 1
          and r["reverse_input_json"] == {"undo": True} and r["reverse_input_hash"] == "rh", r)
    # recovery: re-request with the SAME (deterministic) binding -> already_requested,
    # returning the COMPLETE persisted binding (incl. the durable input JSON the
    # runner must dispatch).
    _, r = rev_req(wf5, tok5, 2, rev2_id, T(5))
    check("reverse_request_idempotent",
          r and r["outcome"] == "already_requested" and r["reverse_invocation_id"] == rev2_id.hex()
          and r["reverse_operation_name"] == "release" and r["reverse_schema_version"] == 1
          and r["reverse_input_json"] == {"undo": True} and r["reverse_input_hash"] == "rh", r)
    # each immutable binding field is pinned INDEPENDENTLY (persisted is
    # rev2_id/"release"/1/{"undo":true}/"rh"): a difference in ANY one alone is a
    # binding_conflict, so dropping a single comparison would be caught.
    _, r = call(cur, "sp_mf_checkpoint_reverse_request",
                (wf5, EXEC, tok5, 2, os.urandom(16), "release", 1, '{"undo":true}', "rh", T(5)))
    check("reverse_request_conflict_id", r and r["outcome"] == "binding_conflict", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_request",
                (wf5, EXEC, tok5, 2, rev2_id, "release-v2", 1, '{"undo":true}', "rh", T(5)))
    check("reverse_request_conflict_name", r and r["outcome"] == "binding_conflict", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_request",
                (wf5, EXEC, tok5, 2, rev2_id, "release", 2, '{"undo":true}', "rh", T(5)))
    check("reverse_request_conflict_version", r and r["outcome"] == "binding_conflict", r)
    # right hash + WRONG json must NOT slip through (hash is caller-asserted, not
    # DB-verified): the JSON CONTENT is compared.
    _, r = call(cur, "sp_mf_checkpoint_reverse_request",
                (wf5, EXEC, tok5, 2, rev2_id, "release", 1, '{"undo":"WRONG"}', "rh", T(5)))
    check("reverse_request_conflict_input", r and r["outcome"] == "binding_conflict", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_request",
                (wf5, EXEC, tok5, 2, rev2_id, "release", 1, '{"undo":true}', "rh2", T(5)))
    check("reverse_request_conflict_hash", r and r["outcome"] == "binding_conflict", r)
    # time discipline on settle: a non-advancing event_ts is rejected
    _, r = call(cur, "sp_mf_checkpoint_reverse_settle", (wf5, EXEC, tok5, 2, rev2_id, '{"released":true}', T(4)))
    check("reverse_settle_skew", r and r["outcome"] == "event_time_skew", r)
    # settle seq 2 -> stack DESCENDS to seq 1 (still reversing, lease retained)
    _, r = call(cur, "sp_mf_checkpoint_reverse_settle", (wf5, EXEC, tok5, 2, rev2_id, '{"released":true}', T(6)))
    check("reverse_settle_descends", r and r["outcome"] == "reversing" and r["next_seq"] == 1, r)
    # the head now projects the next active checkpoint (seq 1), pending
    _, r = call(cur, "sp_mf_checkpoint_reverse_head", (wf5,))
    check("reverse_head_descended", r and r["outcome"] == "pending" and r["seq"] == 1, r)
    # lost-ack retry of an intermediate settle is harmless (effectively-once)
    _, r = call(cur, "sp_mf_checkpoint_reverse_settle", (wf5, EXEC, tok5, 2, rev2_id, '{"released":true}', T(7)))
    check("reverse_settle_idempotent", r and r["outcome"] == "already_reversed", r)
    # compensate the LAST checkpoint (seq 1) -> terminal reversed(5)
    rev_req(wf5, tok5, 1, rev1_id, T(8))
    _, r = call(cur, "sp_mf_checkpoint_reverse_settle", (wf5, EXEC, tok5, 1, rev1_id, '{"released":true}', T(9)))
    check("reverse_settle_reversed", r and r["outcome"] == "reversed", r)
    cur.execute("SELECT w.state, MIN(c.reversal_state), MAX(c.reversal_state) FROM tb_mf_workflow w "
                "JOIN tb_mf_workflow_checkpoint c USING (workflow_id) WHERE w.workflow_id=%s GROUP BY w.state", (wf5,))
    check("reverse_settle_terminal", cur.fetchone() == (5, 2, 2), "expected reversed + ALL checkpoints reversed")
    # the whole stack compensated -> the head reports no active checkpoint
    _, r = call(cur, "sp_mf_checkpoint_reverse_head", (wf5,))
    check("reverse_head_none_active", r and r["outcome"] == "none_active", r)
    # finding 4: the terminal settle cleared the lease — a lost-ack retry must still
    # resolve to already_reversed (lease-independent), not fence_lost.
    _, r = call(cur, "sp_mf_checkpoint_reverse_settle", (wf5, EXEC, tok5, 1, rev1_id, '{"released":true}', T(10)))
    check("reverse_settle_terminal_idempotent", r and r["outcome"] == "already_reversed", r)
    # finding 1: identity is verified BEFORE the replay — a WRONG reverse id after
    # terminal is reverse_id_mismatch, never accepted as the same already_reversed.
    _, r = call(cur, "sp_mf_checkpoint_reverse_settle", (wf5, EXEC, tok5, 1, os.urandom(16), '{"released":true}', T(10)))
    check("reverse_settle_terminal_wrong_id", r and r["outcome"] == "reverse_id_mismatch", r)
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf5,))

    # begin_reversal with NO active checkpoints -> straight to terminal reversed
    wf6 = os.urandom(16)
    failed6 = bytes.fromhex("00000000000000000000000000000e61")
    call(cur, "sp_mf_workflow_create", (wf6, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf6, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok6 = r["fencing_token"]
    _seed_op(wf6, 1, failed6, 1, None)
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf6, EXEC, tok6, 1, failed6, T(2), "nothing_to_compensate"))
    check("begin_reversal_no_checkpoint", r and r["outcome"] == "reversed", r)
    cur.execute("SELECT state, execution_direction FROM tb_mf_workflow WHERE workflow_id=%s", (wf6,))
    check("begin_reversal_no_checkpoint_state", cur.fetchone() == (5, 2), "expected reversed/reverse")
    # replay after TERMINAL reversed -> already_begun with state=5 (lease cleared)
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf6, EXEC, tok6, 1, failed6, T(3), "x"))
    check("begin_reversal_already_begun_reversed",
          r and r["outcome"] == "already_begun" and r["state"] == 5, r)
    for t in ("tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf6,))

    # begin_reversal must prove the durable failed op
    wf8 = os.urandom(16)
    op8 = bytes.fromhex("00000000000000000000000000000e81")
    call(cur, "sp_mf_workflow_create", (wf8, SCRIPT, 1, T(0), T(0), '{"pos":"x"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf8, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok8 = r["fencing_token"]
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf8, EXEC, tok8, 1, os.urandom(16), T(2), "x"))
    check("begin_reversal_op_not_found", r and r["outcome"] == "operation_not_found", r)
    _seed_op(wf8, 1, op8, 2, '{"ok":1}')  # a SETTLED (succeeded) op did not fail
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf8, EXEC, tok8, 1, op8, T(2), "x"))
    check("begin_reversal_op_not_failed", r and r["outcome"] == "operation_not_failed", r)
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf8, EXEC, tok8, 1, os.urandom(16), T(2), "x"))
    check("begin_reversal_op_conflict", r and r["outcome"] == "operation_conflict", r)
    for t in ("tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf8,))

    # compensation that cannot continue -> blocked_resolution(3, reverse) + checkpoint
    # resolution_required(3), lease released, audit event. (Entry into blocked only.)
    wf7 = os.urandom(16)
    cp7_op = bytes.fromhex("00000000000000000000000000000e71")
    failed7 = bytes.fromhex("00000000000000000000000000000e73")
    rev7_id = bytes.fromhex("00000000000000000000000000000e7a")
    call(cur, "sp_mf_workflow_create", (wf7, SCRIPT, 1, T(0), T(0), '{"pos":"op:2:dispatched"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf7, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tok7 = r["fencing_token"]
    _seed_checkpoint(wf7, 1, cp7_op, '{"reservation":"r7"}')
    _seed_op(wf7, 2, failed7, 1, None)
    call(cur, "sp_mf_workflow_begin_reversal", (wf7, EXEC, tok7, 2, failed7, T(3), "forward_op_rejected"))
    # finding 2: cannot block a compensation that was never DISPATCHED (revid NULL)
    _, r = call(cur, "sp_mf_checkpoint_reverse_block", (wf7, EXEC, tok7, 1, rev7_id, 2, "x", T(4)))
    check("reverse_block_not_requested", r and r["outcome"] == "not_requested", r)
    # a stale token cannot drive reversal (checkpoint still active)
    _, r = rev_req(wf7, 999, 1, os.urandom(16), T(4))
    check("reverse_fence_lost", r and r["outcome"] == "fence_lost", r)
    rev_req(wf7, tok7, 1, rev7_id, T(4))
    # time discipline on block
    _, r = call(cur, "sp_mf_checkpoint_reverse_block", (wf7, EXEC, tok7, 1, rev7_id, 2, "x", T(4)))
    check("reverse_block_skew", r and r["outcome"] == "event_time_skew", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_block", (wf7, EXEC, tok7, 1, rev7_id, 2, "compensation_rejected", T(5)))
    check("reverse_block", r and r["outcome"] == "blocked", r)
    cur.execute("SELECT w.state, w.execution_direction, w.current_disposition, w.lease_owner, c.reversal_state "
                "FROM tb_mf_workflow w JOIN tb_mf_workflow_checkpoint c USING (workflow_id) WHERE w.workflow_id=%s", (wf7,))
    check("reverse_block_state", cur.fetchone() == (3, 2, 2, None, 3),
          "expected blocked/reverse/failed/no-lease + checkpoint resolution_required")
    # finding 4: blocking cleared the lease -> replay is lease-independent already_blocked
    _, r = call(cur, "sp_mf_checkpoint_reverse_block", (wf7, EXEC, tok7, 1, rev7_id, 2, "compensation_rejected", T(6)))
    check("reverse_block_idempotent", r and r["outcome"] == "already_blocked", r)
    # the committed begin command is recognized across LATER reverse states: a retry
    # while blocked_resolution returns already_begun(state=3) via the persisted
    # trigger, NOT fence_lost (the reversal began even though the lease is gone).
    _, r = call(cur, "sp_mf_workflow_begin_reversal", (wf7, EXEC, tok7, 2, failed7, T(7), "forward_op_rejected"))
    check("begin_reversal_already_begun_blocked",
          r and r["outcome"] == "already_begun" and r["state"] == 3, r)
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf7,))

    # the durable dispatch deferral also works in the REVERSING direction (used for
    # a Pending/uncertain COMPENSATION dispatch): state stays reversing(2), the lease
    # clears, and the absolute backoff deadline persists.
    wfB = os.urandom(16)
    cpB_op = bytes.fromhex("00000000000000000000000000000eb1")
    failedB = bytes.fromhex("00000000000000000000000000000eb3")
    call(cur, "sp_mf_workflow_create", (wfB, SCRIPT, 1, T(0), T(0), '{"pos":"op:2:dispatched"}', "{}"))
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wfB, EXEC, T(1), "2026-02-01 13:30:00.000000"))
    tokB = r["fencing_token"]
    _seed_checkpoint(wfB, 1, cpB_op, '{"reservation":"rB"}')
    _seed_op(wfB, 2, failedB, 1, None)
    call(cur, "sp_mf_workflow_begin_reversal", (wfB, EXEC, tokB, 2, failedB, T(3), "forward_op_rejected"))
    _, r = call(cur, "sp_mf_operation_dispatch_defer", (wfB, EXEC, tokB, T(4), T(9), T(4), "compensation_pending"))
    check("reversing_dispatch_defer", r and r["outcome"] == "deferred", r)
    cur.execute("SELECT state, lease_owner, next_attempt_at FROM tb_mf_workflow WHERE workflow_id=%s", (wfB,))
    stB, loB, naB = cur.fetchone()
    check("reversing_defer_state",
          stB == 2 and loB is None and naB == datetime.datetime(2026, 2, 1, 13, 0, 9), (stB, loB, naB))
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wfB,))

    # the reverse binding is ALL-OR-NONE: a partial binding (an invocation id with
    # no pinned contract/input) is rejected by the schema CHECK, so reverse_head can
    # never classify a half-written binding as 'dispatched'.
    wf9 = os.urandom(16)
    call(cur, "sp_mf_workflow_create", (wf9, SCRIPT, 1, T(0), T(0), '{"pos":"x"}', "{}"))
    try:
        cur.execute(
            "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, "
            "payload, reversal_state, reverse_invocation_id, created_at, updated_at) "
            "VALUES (%s, 1, 'reserve', %s, '{}', 1, %s, %s, %s)",
            (wf9, bytes.fromhex("00000000000000000000000000000e91"),
             bytes.fromhex("00000000000000000000000000000e9a"), T(2), T(2)))
        check("checkpoint_binding_all_or_none", False, "partial binding INSERT was accepted")
    except pymysql.MySQLError as e:
        check("checkpoint_binding_all_or_none", "ck_mf_checkpoint_reverse_binding" in str(e), e)
    # each VALIDITY rule on the complete-tuple branch is pinned independently, so
    # removing any single rule would be caught.
    def _bad_binding(label, revid, rname, rsv, rhash):
        try:
            cur.execute(
                "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, "
                "payload, reversal_state, reverse_invocation_id, reverse_operation_name, "
                "reverse_schema_version, reverse_input_json, reverse_input_hash, created_at, updated_at) "
                "VALUES (%s, 1, 'reserve', %s, '{}', 1, %s, %s, %s, '{}', %s, %s, %s)",
                (wf9, bytes.fromhex("00000000000000000000000000000e91"),
                 revid, rname, rsv, rhash, T(2), T(2)))
            check(label, False, "degenerate binding INSERT was accepted")
            cur.execute("DELETE FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s", (wf9,))
        except pymysql.MySQLError as e:
            check(label, "ck_mf_checkpoint_reverse_binding" in str(e), e)

    good_id = bytes.fromhex("00000000000000000000000000000e9a")
    _bad_binding("checkpoint_binding_empty_name", good_id, "", 1, "h")
    _bad_binding("checkpoint_binding_empty_hash", good_id, "release", 1, "")
    _bad_binding("checkpoint_binding_bad_version", good_id, "release", 0, "h")
    _bad_binding("checkpoint_binding_short_id", bytes.fromhex("0011"), "release", 1, "h")
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf9,))

    # status/result table invariant: requested(1) + a non-NULL result must be
    # rejected. Include a valid schema_version so the row is well-formed on every
    # OTHER required column — the insert must fail on the status/result CHECK
    # specifically, not on a missing NOT NULL column.
    try:
        cur.execute(
            "INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
            "schema_version, input_json, input_hash, status, result_json, created_at, updated_at) "
            "VALUES (%s, 99, %s, 'x', 1, '{}', 'h', 1, '{\"r\":1}', %s, %s)",
            (WF, bytes.fromhex("00000000000000000000000000000bff"), ts(8), ts(8)))
        check("status_result_invariant", False, "INSERT of requested+result was accepted")
    except pymysql.MySQLError as e:
        check("status_result_invariant", "ck_mf_operation_status_result" in str(e), e)

    # cleanup
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (WF,))
    conn.close()

    total = 93
    print(f"sp_operation regression: {total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
