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
                   (WF, EXEC, None, 1, OPID, "echo-transform", '{"values":[1]}', "h1", '{"pos":"d"}', ts(2), "{}"))
    check("null_fencing_signals", (not ok) and "MfFencingTokenInvalid" in str(err), (ok, err))

    # Valid request.
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", '{"values":[1]}', "h1", '{"pos":"d"}', ts(2), "{}"))
    check("request", r and r["outcome"] == "requested", r)

    # Replay with a different input_hash -> operation_conflict.
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", '{"values":[9]}', "hX", '{"pos":"d"}', ts(3), "{}"))
    check("request_conflict", r and r["outcome"] == "operation_conflict", r)

    # Matching replay -> exists (authoritative id).
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, token, 1, OPID, "echo-transform", '{"values":[1]}', "h1", '{"pos":"d"}', ts(4), "{}"))
    check("request_replay_exists", r and r["outcome"] == "exists" and r["operation_id"] == OPID.hex(), r)

    # Stale token on an EXISTING matching request must FENCE (not 'exists').
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, 999, 1, OPID, "echo-transform", '{"values":[1]}', "h1", '{"pos":"d"}', ts(5), "{}"))
    check("request_existing_stale_token_fence_lost", r and r["outcome"] == "fence_lost", r)

    # Stale token on a NEW operation_seq -> fence_lost.
    _, r = call(cur, "sp_mf_operation_request",
                (WF, EXEC, 999, 2, bytes.fromhex("00000000000000000000000000000b0a"),
                 "echo-transform", '{"values":[2]}', "h2", '{"pos":"d"}', ts(6), "{}"))
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

    # status/result table invariant: requested + result must be rejected.
    try:
        cur.execute(
            "INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
            "input_json, input_hash, status, result_json, created_at, updated_at) "
            "VALUES (%s, 99, %s, 'x', '{}', 'h', 1, '{\"r\":1}', %s, %s)",
            (WF, bytes.fromhex("00000000000000000000000000000bff"), ts(8), ts(8)))
        check("status_result_invariant", False, "INSERT of requested+result was accepted")
    except pymysql.MySQLError:
        check("status_result_invariant", True)

    # cleanup
    for t in ("tb_mf_workflow_checkpoint", "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (WF,))
    conn.close()

    total = 15
    print(f"sp_operation regression: {total - len(failures)}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
