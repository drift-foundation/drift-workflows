#!/usr/bin/env python3
"""Focused SQL regression for the composition (1b.1) workflow-call stored procedures.

Covers the hardening invariants:
  - sp_mf_call_submit creates the full atomic bundle (parent op, child workflow/plan/args/created
    event, sidecar, parent continuation/event) in one transaction
  - malformed child inputs SIGNAL before any mutation (arg-shape checks run before the fence,
    plan-conformance, idempotency, and recursion-guard phases)
  - the recursion guard (call_cycle / max_call_depth_exceeded) rejects with ZERO partial rows —
    the whole point of this procedure's strict validate-then-mutate phasing
  - idempotent replay (already_submitted) vs immutable-field mismatch (call_conflict)
  - sp_mf_call_inspect is a PURE read (child's authoritative state/return + the sidecar hint)
  - sp_mf_call_hint_refresh is best-effort and monotonic by time (never clobbers a fresher hint)
  - sp_mf_child_terminal_notify only ever touches the hint + the parent's next_attempt_at (pulled
    earlier, never later) — it must NEVER settle/fail/touch the parent's own state
  - sp_mf_checkpoint_reverse_head's Pending outcome carries call_kind
  - sp_mf_checkpoint_reverse_noop reverses a call checkpoint and rejects a participant one

Run via the mariachi venv python (has PyMySQL) — mirrors microflows/justfile `_test-sp`'s
invocation of db-tests/sp_operation_test.py. Requires the `microflows` schema loaded
(`just db-load-schema`) and MDB_ROOT_PWD.
"""
import os
import sys
import uuid

import pymysql

HOST = os.environ.get("DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("DB_PORT", "34214"))
USER = os.environ.get("DB_USER", "root")
PWD = os.environ.get("MDB_ROOT_PWD", "rootpw")

EXEC = bytes.fromhex("e9000000000000000000000000000019")
SCRIPT = f"sp-call-test-{uuid.uuid4().hex[:8]}"

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


def call(cur, proc, args):
    """Call an SP returning one JSON `result`; return (ok, value_or_error)."""
    try:
        cur.execute(f"CALL {proc}({','.join(['%s'] * len(args))})", args)
        row = cur.fetchone()
        return True, __import__("json").loads(row[0]) if row else None
    except pymysql.MySQLError as e:
        return False, e


def main():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD,
                            database="microflows", autocommit=True)
    cur = conn.cursor()

    T = lambda s: f"2026-03-01 09:00:{s:02d}.000000"  # noqa: E731

    all_workflow_ids = []

    def new_parent(plan_length, ver="1.0.0", phash=None):
        """Create + claim a fresh planned parent workflow; return (workflow_id, token)."""
        wf = os.urandom(16)
        all_workflow_ids.append(wf)
        h = phash or bytes.fromhex("01" + "a1" * 32)
        call(cur, "sp_mf_workflow_create_planned",
             (wf, SCRIPT, ver, T(0), T(0), '{"pos":"start"}', "{}", h, plan_length, b"{}"))
        _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf, EXEC, T(1), "2026-03-01 09:30:00.000000"))
        return wf, r["fencing_token"]

    _UNSET = object()  # distinguishes "caller didn't pass this" from "caller passed None/''/0"

    def submit(wf, tok, seq, child_id, input_json="{}", input_hash="ih", new_cont='{"pos":"after"}',
               event_ts=_UNSET, event_payload="{}", child_script=_UNSET, child_ver="2.0.0",
               child_hash=_UNSET, child_plen=1, child_cont='{"pos":"child_start"}',
               child_next_at=_UNSET, child_event_payload='{"note":"created"}', node_id="call_1",
               max_depth=16):
        if child_script is _UNSET: child_script = f"sp-call-child-{uuid.uuid4().hex[:8]}"
        if child_hash is _UNSET: child_hash = bytes.fromhex("01" + "c1" * 32)
        if event_ts is _UNSET: event_ts = T(2)
        if child_next_at is _UNSET: child_next_at = event_ts
        return call(cur, "sp_mf_call_submit",
                     (wf, EXEC, tok, seq, child_id,
                      input_json, input_hash, new_cont, event_ts, event_payload,
                      child_id, child_script, child_ver, child_hash, child_plen,
                      child_cont, child_next_at, child_event_payload, node_id, max_depth))

    # ================================================================
    # 1. Fresh submit creates the full atomic bundle.
    # ================================================================
    wf1, tok1 = new_parent(plan_length=1)
    child1 = os.urandom(16)
    all_workflow_ids.append(child1)
    child1_script = f"sp-call-child-{uuid.uuid4().hex[:8]}"
    child1_hash = bytes.fromhex("01" + "c1" * 32)
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount":10}', input_hash="ih1",
                  new_cont='{"pos":"after_call"}', event_ts=T(2), event_payload='{"k":"parent"}',
                  child_script=child1_script, child_ver="2.0.0", child_hash=child1_hash, child_plen=2,
                  child_cont='{"pos":"child_start"}', child_next_at=T(2),
                  child_event_payload='{"k":"child"}', node_id="call_1", max_depth=16)
    check("submit_fresh", r and r["outcome"] == "submitted" and r["child_workflow_id"] == child1.hex(), r)

    cur.execute("SELECT call_kind, operation_id, operation_name, schema_version, status, result_json, input_hash "
                "FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (wf1,))
    row = cur.fetchone()
    check("submit_op_row", row == (2, child1, child1_script, 1, 1, None, "ih1"), row)

    cur.execute("SELECT script_name, state, execution_direction, parent_workflow_id, parent_node_id, "
                "root_workflow_id, call_depth, continuation, next_attempt_at "
                "FROM tb_mf_workflow WHERE workflow_id=%s", (child1,))
    row = cur.fetchone()
    check("submit_child_workflow_row",
          row[0] == child1_script and row[1] == 1 and row[2] == 1 and row[3] == wf1
          and row[4] == "call_1" and row[5] == wf1 and row[6] == 1
          and row[7] == '{"pos":"child_start"}', row)

    cur.execute("SELECT plan_version, content_hash, plan_length FROM tb_mf_workflow_plan WHERE workflow_id=%s",
                (child1,))
    row = cur.fetchone()
    check("submit_child_plan_row", row == ("2.0.0", child1_hash, 2), row)

    cur.execute("SELECT args_canonical FROM tb_mf_workflow_args WHERE workflow_id=%s", (child1,))
    row = cur.fetchone()
    check("submit_child_args_row", row and bytes(row[0]) == b'{"amount":10}', row)

    cur.execute("SELECT event_seq, kind, payload FROM tb_mf_workflow_event WHERE workflow_id=%s", (child1,))
    row = cur.fetchone()
    check("submit_child_created_event", row == (1, "created", '{"k":"child"}'), row)

    cur.execute("SELECT child_workflow_id, child_script_name, child_plan_version, child_content_hash, "
                "child_status, first_requested_at FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1", (wf1,))
    row = cur.fetchone()
    check("submit_sidecar_row",
          row[0] == child1 and row[1] == child1_script and row[2] == "2.0.0" and row[3] == child1_hash
          and row[4] == 1, row)

    cur.execute("SELECT continuation, current_event_seq FROM tb_mf_workflow WHERE workflow_id=%s", (wf1,))
    row = cur.fetchone()
    check("submit_parent_continuation_advanced", row == ('{"pos":"after_call"}', 2), row)

    cur.execute("SELECT event_seq, kind, actor, payload FROM tb_mf_workflow_event "
                "WHERE workflow_id=%s AND event_seq=2", (wf1,))
    row = cur.fetchone()
    check("submit_parent_event_appended", row == (2, "call_submitted", EXEC, '{"k":"parent"}'), row)

    # ================================================================
    # 2. Malformed child inputs SIGNAL before any mutation.
    # ================================================================
    wf_bad, tok_bad = new_parent(plan_length=5)

    def expect_signal_no_rows(name, expected, **kwargs):
        bad_child = os.urandom(16)
        ok, err = submit(wf_bad, tok_bad, 1, bad_child, **kwargs)
        check(name, (not ok) and expected in str(err), (ok, err))
        cur.execute("SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (wf_bad,))
        check(f"{name}_no_op_row", cur.fetchone()[0] == 0, "expected zero op rows after SIGNAL")
        cur.execute("SELECT COUNT(*) FROM tb_mf_workflow WHERE workflow_id=%s", (bad_child,))
        check(f"{name}_no_child_row", cur.fetchone()[0] == 0, "expected zero child rows after SIGNAL")

    expect_signal_no_rows("bad_child_plan_length", "MfChildPlanLengthInvalid", child_plen=0)
    expect_signal_no_rows("bad_child_continuation", "MfChildContinuationInvalid", child_cont="[1,2]")
    expect_signal_no_rows("bad_child_event_payload", "MfChildEventPayloadInvalid", child_event_payload="[1,2]")
    expect_signal_no_rows("bad_child_next_attempt_at", "MfChildNextAttemptAtInvalid", child_next_at=None)
    expect_signal_no_rows("bad_child_script_name", "MfChildScriptNameInvalid", child_script="")
    expect_signal_no_rows("bad_child_plan_version", "MfChildPlanVersionInvalid", child_ver="1.2")
    expect_signal_no_rows("bad_child_content_hash", "MfChildContentHashInvalid",
                           child_hash=bytes.fromhex("01" + "c1" * 10))
    expect_signal_no_rows("bad_input_json_non_object", "MfInputJsonInvalid", input_json="[1,2]")
    # operation_id/child_workflow_id mismatch needs its own explicit call (the submit() wrapper
    # always sets them equal by construction, so it can't exercise this SIGNAL).
    bad_child2 = os.urandom(16)
    ok, err = call(cur, "sp_mf_call_submit",
                    (wf_bad, EXEC, tok_bad, 1, os.urandom(16),  # operation_id != bad_child2
                     "{}", "ih", '{"pos":"after"}', T(2), "{}",
                     bad_child2, f"c-{uuid.uuid4().hex[:6]}", "1.0.0", bytes.fromhex("01" + "c1" * 32), 1,
                     '{"pos":"start"}', T(2), "{}", "call_1", 16))
    check("bad_operation_id_child_mismatch_signals", (not ok) and "MfOperationIdChildMismatch" in str(err), (ok, err))
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow WHERE workflow_id=%s", (bad_child2,))
    check("bad_operation_id_child_mismatch_no_child_row", cur.fetchone()[0] == 0, "expected zero rows")

    # ================================================================
    # 3. Recursion / depth rejection leaves ZERO partial rows.
    # ================================================================
    # call_cycle: the child's plan identity equals the PARENT's OWN identity (a 1-hop self-cycle —
    # the ancestor set's hops=0 entry IS the parent itself).
    cyc_ver = "3.1.4"
    cyc_hash = bytes.fromhex("01" + "d1" * 32)
    wf_cyc, tok_cyc = new_parent(plan_length=1, ver=cyc_ver, phash=cyc_hash)
    cyc_child = os.urandom(16)
    _, r = submit(wf_cyc, tok_cyc, 1, cyc_child, child_script=SCRIPT, child_ver=cyc_ver, child_hash=cyc_hash)
    check("call_cycle_rejected", r and r["outcome"] == "call_rejected" and r["reason"] == "call_cycle", r)
    cur.execute("SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (wf_cyc,))
    check("call_cycle_no_op_row", cur.fetchone()[0] == 0, "expected zero op rows")
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow WHERE workflow_id=%s", (cyc_child,))
    check("call_cycle_no_child_row", cur.fetchone()[0] == 0, "expected zero child rows")
    cur.execute("SELECT COUNT(*) FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1", (wf_cyc,))
    check("call_cycle_no_sidecar_row", cur.fetchone()[0] == 0, "expected zero sidecar rows")

    # max_call_depth_exceeded: a genuine 2-hop nested call with a small max_call_depth.
    wf_da, tok_da = new_parent(plan_length=1)
    wf_db = os.urandom(16)
    all_workflow_ids.append(wf_db)
    _, r = submit(wf_da, tok_da, 1, wf_db, child_plen=1, max_depth=16)
    check("depth_setup_nested_child", r and r["outcome"] == "submitted", r)
    _, r = call(cur, "sp_mf_workflow_claim_by_id", (wf_db, EXEC, T(3), "2026-03-01 09:30:00.000000"))
    tok_db = r["fencing_token"]
    wf_dc = os.urandom(16)
    _, r = submit(wf_db, tok_db, 1, wf_dc, event_ts=T(4), max_depth=1)
    check("max_call_depth_exceeded", r and r["outcome"] == "call_rejected"
          and r["reason"] == "max_call_depth_exceeded", r)
    cur.execute("SELECT COUNT(*) FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (wf_db,))
    check("max_call_depth_no_op_row", cur.fetchone()[0] == 0, "expected zero op rows")
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow WHERE workflow_id=%s", (wf_dc,))
    check("max_call_depth_no_child_row", cur.fetchone()[0] == 0, "expected zero child rows")

    # ================================================================
    # 4. Idempotent replay -> already_submitted.
    # ================================================================
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount":10}', input_hash="ih1",
                  new_cont='{"pos":"after_call"}', event_ts=T(2), event_payload='{"k":"parent"}',
                  child_script=child1_script, child_ver="2.0.0", child_hash=child1_hash, child_plen=2,
                  child_cont='{"pos":"child_start"}', child_next_at=T(2),
                  child_event_payload='{"k":"child"}', node_id="call_1", max_depth=16)
    check("submit_replay_already_submitted",
          r and r["outcome"] == "already_submitted" and r["child_workflow_id"] == child1.hex(), r)

    # ================================================================
    # 5. Immutable mismatch -> call_conflict.
    # ================================================================
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount":10}', input_hash="DIFFERENT_HASH",
                  child_script=child1_script, child_ver="2.0.0", child_hash=child1_hash, child_plen=2)
    check("submit_conflict_input_hash", r and r["outcome"] == "call_conflict", r)
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount":10}', input_hash="ih1",
                  child_script=child1_script, child_ver="9.9.9", child_hash=child1_hash, child_plen=2)
    check("submit_conflict_plan_version", r and r["outcome"] == "call_conflict", r)
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount":999}', input_hash="ih1",
                  child_script=child1_script, child_ver="2.0.0", child_hash=child1_hash, child_plen=2)
    check("submit_conflict_args", r and r["outcome"] == "call_conflict", r)
    # The durable plan pin is (script_name, plan_version, content_hash, plan_length) — plan_length
    # lives only on tb_mf_workflow_plan and must be compared on replay too, not just the sidecar's
    # script/version/hash triple.
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount":10}', input_hash="ih1",
                  child_script=child1_script, child_ver="2.0.0", child_hash=child1_hash, child_plen=99)
    check("submit_conflict_plan_length", r and r["outcome"] == "call_conflict", r)
    # arg_input_json is trusted as ALREADY CANONICAL — this procedure does NOT canonicalize or
    # normalize it. A byte-different-but-semantically-equal JSON document (extra whitespace) must
    # NOT replay as already_submitted; it is a genuine args mismatch (call_conflict), pinning that
    # canonicalization is the (future) host wrapper's responsibility, never this SP's.
    _, r = submit(wf1, tok1, 1, child1, input_json='{"amount": 10}', input_hash="ih1",
                  child_script=child1_script, child_ver="2.0.0", child_hash=child1_hash, child_plen=2)
    check("submit_noncanonical_json_not_idempotent", r and r["outcome"] == "call_conflict", r)

    # ================================================================
    # 6. sp_mf_call_inspect is a PURE read.
    # ================================================================
    cur.execute("SELECT child_status, last_inspected_at, updated_at FROM tb_mf_call "
                "WHERE workflow_id=%s AND operation_seq=1", (wf1,))
    before = cur.fetchone()
    _, r = call(cur, "sp_mf_call_inspect", (wf1, 1))
    check("call_inspect_found", r and r["outcome"] == "found" and r["child_workflow_id"] == child1.hex()
          and r["state"] == 1 and r["is_terminal"] == 0 and r["child_status"] == 1, r)
    _, r2 = call(cur, "sp_mf_call_inspect", (wf1, 1))
    check("call_inspect_repeatable", r2 == r, (r, r2))
    cur.execute("SELECT child_status, last_inspected_at, updated_at FROM tb_mf_call "
                "WHERE workflow_id=%s AND operation_seq=1", (wf1,))
    after = cur.fetchone()
    check("call_inspect_pure_read_no_mutation", before == after, (before, after))
    _, r = call(cur, "sp_mf_call_inspect", (wf1, 99))
    check("call_inspect_not_found", r and r["outcome"] == "not_found", r)

    # ================================================================
    # 7. sp_mf_call_hint_refresh is best-effort and monotonic.
    # ================================================================
    _, r = call(cur, "sp_mf_call_hint_refresh", (wf1, 1, 4, T(10)))
    check("hint_refresh_updates", r and r["outcome"] == "refreshed", r)
    cur.execute("SELECT child_status, last_inspected_at FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1",
                (wf1,))
    row = cur.fetchone()
    check("hint_refresh_applied", row[0] == 4 and str(row[1]) == "2026-03-01 09:00:10", row)

    # An earlier event_ts + a different status must be a no-op (monotonic, no clobber).
    _, r = call(cur, "sp_mf_call_hint_refresh", (wf1, 1, 2, T(5)))
    check("hint_refresh_stale_noop_outcome", r and r["outcome"] == "refreshed", r)
    cur.execute("SELECT child_status, last_inspected_at FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1",
                (wf1,))
    row = cur.fetchone()
    check("hint_refresh_stale_no_clobber", row[0] == 4 and str(row[1]) == "2026-03-01 09:00:10", row)

    # A later event_ts DOES apply.
    _, r = call(cur, "sp_mf_call_hint_refresh", (wf1, 1, 2, T(15)))
    cur.execute("SELECT child_status, last_inspected_at FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1",
                (wf1,))
    row = cur.fetchone()
    check("hint_refresh_later_applies", row[0] == 2 and str(row[1]) == "2026-03-01 09:00:15", row)

    _, r = call(cur, "sp_mf_call_hint_refresh", (wf1, 99, 2, T(16)))
    check("hint_refresh_not_found", r and r["outcome"] == "not_found", r)

    # ================================================================
    # 8. sp_mf_child_terminal_notify: hint + wake ONLY, never settles the parent.
    # ================================================================
    wf8, tok8 = new_parent(plan_length=1)
    child8 = os.urandom(16)
    far_future = "2026-03-01 23:59:59.000000"
    _, r = submit(wf8, tok8, 1, child8, child_next_at=T(2), max_depth=16)
    check("notify_setup_submit", r and r["outcome"] == "submitted", r)
    # Move the parent's own next_attempt_at far into the future (release under fence), so
    # notify's "pull earlier" effect is observable.
    _, r = call(cur, "sp_mf_workflow_release", (wf8, EXEC, tok8, T(3), far_future))
    check("notify_setup_release", r and r["outcome"] == "released", r)

    cur.execute("SELECT state, current_disposition, current_event_seq FROM tb_mf_workflow WHERE workflow_id=%s",
                (wf8,))
    parent_before = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (wf8,))
    events_before = cur.fetchone()[0]
    cur.execute("SELECT status, result_json FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (wf8,))
    op_before = cur.fetchone()

    _, r = call(cur, "sp_mf_child_terminal_notify", (child8, 2, T(4)))
    check("notify_completed", r and r["outcome"] == "notified", r)

    cur.execute("SELECT child_status, last_inspected_at FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1",
                (wf8,))
    row = cur.fetchone()
    check("notify_hint_updated", row[0] == 2 and str(row[1]) == "2026-03-01 09:00:04", row)

    cur.execute("SELECT next_attempt_at FROM tb_mf_workflow WHERE workflow_id=%s", (wf8,))
    row = cur.fetchone()
    check("notify_wake_pulled_earlier", str(row[0]) == "2026-03-01 09:00:04", row)

    cur.execute("SELECT state, current_disposition, current_event_seq FROM tb_mf_workflow WHERE workflow_id=%s",
                (wf8,))
    parent_after = cur.fetchone()
    check("notify_never_settles_parent_state", parent_before == parent_after, (parent_before, parent_after))
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (wf8,))
    events_after = cur.fetchone()[0]
    check("notify_never_appends_parent_event", events_before == events_after, (events_before, events_after))
    cur.execute("SELECT status, result_json FROM tb_mf_operation WHERE workflow_id=%s AND operation_seq=1", (wf8,))
    op_after = cur.fetchone()
    check("notify_never_settles_call_op", op_before == op_after, (op_before, op_after))

    _, r = call(cur, "sp_mf_child_terminal_notify", (os.urandom(16), 2, T(5)))
    check("notify_not_found", r and r["outcome"] == "not_found", r)

    # ================================================================
    # 9. sp_mf_checkpoint_reverse_head returns call_kind.
    # ================================================================
    wf9 = os.urandom(16)
    all_workflow_ids.append(wf9)
    call(cur, "sp_mf_workflow_create", (wf9, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}"))
    call9_op = os.urandom(16)
    cur.execute("INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
                "schema_version, input_json, input_hash, call_kind, status, result_json, created_at, updated_at) "
                "VALUES (%s,1,%s,%s,1,'{}','h',2,2,'{}',%s,%s)", (wf9, call9_op, "some-child-script", T(1), T(1)))
    cur.execute("INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, payload, "
                "reversal_state, created_at, updated_at) VALUES (%s,1,%s,%s,'{}',1,%s,%s)",
                (wf9, "some-child-script", call9_op, T(1), T(1)))
    _, r = call(cur, "sp_mf_checkpoint_reverse_head", (wf9,))
    check("reverse_head_call_kind_present", r and r["outcome"] == "pending" and r["call_kind"] == 2, r)

    wf9b = os.urandom(16)
    all_workflow_ids.append(wf9b)
    call(cur, "sp_mf_workflow_create", (wf9b, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}"))
    part_op = os.urandom(16)
    cur.execute("INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
                "schema_version, input_json, input_hash, call_kind, status, result_json, created_at, updated_at) "
                "VALUES (%s,1,%s,'participant-op',1,'{}','h',1,2,'{}',%s,%s)", (wf9b, part_op, T(1), T(1)))
    cur.execute("INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, payload, "
                "reversal_state, created_at, updated_at) VALUES (%s,1,'participant-op',%s,'{}',1,%s,%s)",
                (wf9b, part_op, T(1), T(1)))
    _, r = call(cur, "sp_mf_checkpoint_reverse_head", (wf9b,))
    check("reverse_head_call_kind_participant", r and r["outcome"] == "pending" and r["call_kind"] == 1, r)

    # ================================================================
    # 10. sp_mf_checkpoint_reverse_noop reverses call checkpoints, rejects participant ones.
    # ================================================================
    # Put wf9 into reversing(2) with a valid fencing token, so reverse_noop's fence check passes.
    cur.execute("UPDATE tb_mf_workflow SET state=2, execution_direction=2, current_disposition=2, "
                "lease_owner=%s, lease_expires_at=%s, fencing_token=77 WHERE workflow_id=%s",
                (EXEC, "2026-03-01 10:00:00.000000", wf9))
    _, r = call(cur, "sp_mf_checkpoint_reverse_noop", (wf9, EXEC, 999, 1, T(6)))
    check("reverse_noop_fence_lost", r and r["outcome"] == "fence_lost", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_noop", (wf9, EXEC, 77, 1, T(6)))
    check("reverse_noop_reverses_call_checkpoint", r and r["outcome"] == "reversed", r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (wf9,))
    check("reverse_noop_checkpoint_state", cur.fetchone()[0] == 2, "expected reversal_state=2")
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (wf9,))
    check("reverse_noop_workflow_reversed", cur.fetchone()[0] == 5, "expected workflow state=5 (reversed)")
    # Idempotent replay.
    _, r = call(cur, "sp_mf_checkpoint_reverse_noop", (wf9, EXEC, 77, 1, T(7)))
    check("reverse_noop_already_reversed", r and r["outcome"] == "already_reversed", r)

    # Participant checkpoint: rejected outright.
    cur.execute("UPDATE tb_mf_workflow SET state=2, execution_direction=2, current_disposition=2, "
                "lease_owner=%s, lease_expires_at=%s, fencing_token=88 WHERE workflow_id=%s",
                (EXEC, "2026-03-01 10:00:00.000000", wf9b))
    _, r = call(cur, "sp_mf_checkpoint_reverse_noop", (wf9b, EXEC, 88, 1, T(6)))
    check("reverse_noop_rejects_participant_checkpoint", r and r["outcome"] == "not_call_checkpoint", r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (wf9b,))
    check("reverse_noop_participant_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged (1)")

    # Out-of-order: a checkpoint below the current top must reject.
    wf10 = os.urandom(16)
    all_workflow_ids.append(wf10)
    call(cur, "sp_mf_workflow_create", (wf10, SCRIPT, 1, T(0), T(0), '{"pos":"start"}', "{}"))
    op10a, op10b = os.urandom(16), os.urandom(16)
    cur.execute("INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
                "schema_version, input_json, input_hash, call_kind, status, result_json, created_at, updated_at) "
                "VALUES (%s,1,%s,'child-a',1,'{}','h',2,2,'{}',%s,%s),"
                "(%s,2,%s,'child-b',1,'{}','h',2,2,'{}',%s,%s)",
                (wf10, op10a, T(1), T(1), wf10, op10b, T(1), T(1)))
    cur.execute("INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, payload, "
                "reversal_state, created_at, updated_at) VALUES "
                "(%s,1,'child-a',%s,'{}',1,%s,%s),(%s,2,'child-b',%s,'{}',1,%s,%s)",
                (wf10, op10a, T(1), T(1), wf10, op10b, T(1), T(1)))
    cur.execute("UPDATE tb_mf_workflow SET state=2, execution_direction=2, current_disposition=2, "
                "lease_owner=%s, lease_expires_at=%s, fencing_token=55 WHERE workflow_id=%s",
                (EXEC, "2026-03-01 10:00:00.000000", wf10))
    _, r = call(cur, "sp_mf_checkpoint_reverse_noop", (wf10, EXEC, 55, 1, T(6)))
    check("reverse_noop_out_of_order", r and r["outcome"] == "out_of_order" and r["top_seq"] == 2, r)
    # Reversing the true top (seq 2) then descends (stays reversing, more to go).
    _, r = call(cur, "sp_mf_checkpoint_reverse_noop", (wf10, EXEC, 55, 2, T(6)))
    check("reverse_noop_descends", r and r["outcome"] == "reversing" and r["next_seq"] == 1, r)

    # ================================================================
    # Cleanup.
    # ================================================================
    for t in ("tb_mf_call", "tb_mf_workflow_args", "tb_mf_workflow_plan", "tb_mf_workflow_checkpoint",
              "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        for w in all_workflow_ids:
            cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (w,))
    conn.close()

    EXPECTED_CHECKS = 79
    total = passed + len(failures)
    if total != EXPECTED_CHECKS:
        failures.append(f"completeness_guard: ran {total} checks, expected {EXPECTED_CHECKS}")
    print(f"sp_call regression: {passed}/{total} passed (expected {EXPECTED_CHECKS})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
