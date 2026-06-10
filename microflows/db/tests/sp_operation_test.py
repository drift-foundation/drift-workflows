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
                (WF, EXEC, token, 1, WRONG_OPID, 1, '{"sum":1}', '{"sum":1}', '{"pos":"done"}', ts(7), "{}"))
    check("settle_wrong_opid_conflict", r and r["outcome"] == "operation_conflict", r)

    # Settle (correct id), then repeated settle is idempotent.
    _, r = call(cur, "sp_mf_operation_settle",
                (WF, EXEC, token, 1, OPID, 1, '{"sum":1}', '{"sum":1}', '{"pos":"done"}', ts(8), "{}"))
    check("settle", r and r["outcome"] == "settled" and r["result"] == {"sum": 1}, r)

    _, r = call(cur, "sp_mf_operation_settle",
                (WF, EXEC, token, 1, OPID, 1, '{"sum":1}', '{"sum":1}', '{"pos":"done"}', ts(9), "{}"))
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
                (wf3, EXEC, tok3, 1, OPID3, 1, '{"sum":2}', '{"sum":2}', '{"pos":"done"}', T(2), "{}"))
    check("settle_skew_defer_until", r and r["outcome"] == "event_time_skew"
          and r["defer_until"] == "2026-02-01 13:00:07.000000", r)

    # release with a wrong token -> fence_lost (the runner maps this to a distinct
    # defer_failed, never reporting a committed defer).
    _, r = call(cur, "sp_mf_workflow_release", (wf3, EXEC, 999, T(3), T(4)))
    check("release_fence_lost", r and r["outcome"] == "fence_lost", r)
    for t in ("tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (wf3,))

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

    total = 35
    print(f"sp_operation regression: {total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
