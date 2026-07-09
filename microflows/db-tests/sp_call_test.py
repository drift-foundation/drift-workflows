#!/usr/bin/env python3
"""Focused SQL regression for the composition (1b.1/1c) workflow-call stored procedures.

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
  - sp_mf_checkpoint_reverse_child_reopen ("T1", 1c): completed(4)->reversing(2) reopen is an
    explicit parent+child transaction (dual event append, dual event-time-skew check); idempotent
    on child state 2/3/5/6; failed(7) is diagnosed as child_state_inconsistent, never a soft skip
  - sp_mf_checkpoint_reverse_child_settle (1c): independently verifies the child reached
    reversed(5)/resolved_exception(6) before flipping the parent's checkpoint -- refuses
    reversing/blocked_resolution (child_not_terminal) and completed/failed (child_not_compensated);
    descend/terminal mechanics are unchanged from the retired sp_mf_checkpoint_reverse_noop

Run via the mariachi venv python (has PyMySQL) — mirrors microflows/justfile `_test-sp`'s
invocation of db-tests/sp_operation_test.py. Requires the `microflows` schema loaded
(`just db-load-schema`) and MDB_ROOT_PWD.
"""
import json
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

    # (state, disposition, direction) per tb_mf_workflow's own CHECK constraints -- used by
    # make_child_row below to construct a standalone child row directly in a given state, for
    # testing sp_mf_checkpoint_reverse_child_reopen/_settle's child-state branching in isolation
    # (without driving a real child through its own forward/reverse path). state=1 (forward) is
    # included even though it should be structurally impossible for a call checkpoint's child
    # (which is always completed(4) at checkpoint-creation time) -- settle must still diagnose it
    # rather than fall through and settle the parent as if compensated (review-caught regression).
    _DISP_DIR_BY_STATE = {1: (0, 1), 4: (1, 1), 2: (2, 2), 3: (2, 2), 5: (2, 2), 6: (2, 2), 7: (2, 2)}

    def make_child_row(state, event_ts=None, with_checkpoint=True, checkpoint_seq=1,
                        fencing_token=1):
        wf = os.urandom(16)
        all_workflow_ids.append(wf)
        if event_ts is None:
            event_ts = T(1)
        disp, direction = _DISP_DIR_BY_STATE[state]
        ret = "{}" if state == 4 else None
        term_reason = None if state in (1, 4) else "child-own-reason"
        cur.execute(
            "INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
            "execution_direction, current_disposition, current_event_ts, "
            "fencing_token, lease_owner, lease_expires_at, next_attempt_at, current_operation_attempt, "
            "continuation, terminal_reason, workflow_return_json, created_at, updated_at) "
            "VALUES (%s,%s,1,%s,%s,%s,%s,%s,NULL,NULL,%s,0,'{\"pos\":\"x\"}',%s,%s,%s,%s)",
            (wf, SCRIPT, state, direction, disp, event_ts, fencing_token, event_ts,
             term_reason, ret, event_ts, event_ts))
        if with_checkpoint:
            op_id = os.urandom(16)
            cur.execute(
                "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, "
                "payload, reversal_state, created_at, updated_at) VALUES (%s,%s,'child-op',%s,'{}',1,%s,%s)",
                (wf, checkpoint_seq, op_id, event_ts, event_ts))
        return wf

    def make_parent_call_checkpoint(child_wf, seq, fencing_token, parent_event_ts=None):
        """A parent workflow already in reversing(2) with a call_kind=2 checkpoint at `seq`,
        sidecar-linked to `child_wf` -- the fixture sp_mf_checkpoint_reverse_child_reopen/_settle
        operate on."""
        wf = os.urandom(16)
        all_workflow_ids.append(wf)
        if parent_event_ts is None:
            parent_event_ts = T(1)
        cur.execute(
            "INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
            "execution_direction, current_disposition, current_event_ts, "
            "fencing_token, lease_owner, lease_expires_at, next_attempt_at, current_operation_attempt, "
            "continuation, created_at, updated_at) "
            "VALUES (%s,%s,1,2,2,2,%s,%s,%s,%s,%s,0,'{\"pos\":\"reverse\"}',%s,%s)",
            (wf, SCRIPT, parent_event_ts, fencing_token, EXEC,
             "2026-03-01 10:00:00.000000", parent_event_ts, parent_event_ts, parent_event_ts))
        op_id = os.urandom(16)
        cur.execute(
            "INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
            "schema_version, input_json, input_hash, call_kind, status, result_json, created_at, updated_at) "
            "VALUES (%s,%s,%s,'child-script',1,'{}','h',2,2,'{}',%s,%s)",
            (wf, seq, op_id, parent_event_ts, parent_event_ts))
        cur.execute(
            "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, "
            "payload, reversal_state, created_at, updated_at) VALUES (%s,%s,'child-script',%s,'{}',1,%s,%s)",
            (wf, seq, op_id, parent_event_ts, parent_event_ts))
        cur.execute(
            "INSERT INTO tb_mf_call (workflow_id, operation_seq, child_workflow_id, child_script_name, "
            "child_plan_version, child_content_hash, child_status, first_requested_at, created_at, updated_at) "
            "VALUES (%s,%s,%s,'child-script','1.0.0',%s,2,%s,%s,%s)",
            (wf, seq, child_wf, bytes.fromhex("01" + "e1" * 32), parent_event_ts, parent_event_ts, parent_event_ts))
        return wf

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

    cur.execute("SELECT kind, payload FROM tb_mf_workflow_event WHERE workflow_id=%s", (child1,))
    row = cur.fetchone()
    check("submit_child_created_event", row == ("created", '{"k":"child"}'), row)

    cur.execute("SELECT child_workflow_id, child_script_name, child_plan_version, child_content_hash, "
                "child_status, first_requested_at FROM tb_mf_call WHERE workflow_id=%s AND operation_seq=1", (wf1,))
    row = cur.fetchone()
    check("submit_sidecar_row",
          row[0] == child1 and row[1] == child1_script and row[2] == "2.0.0" and row[3] == child1_hash
          and row[4] == 1, row)

    cur.execute("SELECT continuation, current_event_ts FROM tb_mf_workflow WHERE workflow_id=%s", (wf1,))
    row = cur.fetchone()
    check("submit_parent_continuation_advanced",
          row[0] == '{"pos":"after_call"}' and str(row[1]) == "2026-03-01 09:00:02", row)

    cur.execute("SELECT kind, actor, payload FROM tb_mf_workflow_event "
                "WHERE workflow_id=%s AND kind='call_submitted'", (wf1,))
    row = cur.fetchone()
    check("submit_parent_event_appended", row == ("call_submitted", EXEC, '{"k":"parent"}'), row)

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

    cur.execute("SELECT state, current_disposition, current_event_ts FROM tb_mf_workflow WHERE workflow_id=%s",
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

    cur.execute("SELECT state, current_disposition, current_event_ts FROM tb_mf_workflow WHERE workflow_id=%s",
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
    # 10. sp_mf_checkpoint_reverse_child_reopen ("T1") -- fresh reopen is a parent+child
    #     transaction, exactly-once idempotent, diagnoses failed(7), dual event-time skew.
    # ================================================================
    child_a = make_child_row(state=4, event_ts=T(10), checkpoint_seq=1, fencing_token=1)
    parent_a = make_parent_call_checkpoint(child_a, seq=1, fencing_token=701, parent_event_ts=T(9))

    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (parent_a,))
    parent_events_before = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (child_a,))
    child_events_before = cur.fetchone()[0]

    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (parent_a, EXEC, 999999, 1, T(11)))
    check("reopen_fence_lost", r and r["outcome"] == "fence_lost", r)

    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (parent_a, EXEC, 701, 1, T(11)))
    check("reopen_fresh", r and r["outcome"] == "reopened" and r["child_workflow_id"] == child_a.hex(), r)

    cur.execute("SELECT state, execution_direction, current_disposition, continuation, terminal_reason, "
                "next_attempt_at, fencing_token FROM tb_mf_workflow WHERE workflow_id=%s", (child_a,))
    row = cur.fetchone()
    check("reopen_child_state", row[0] == 2, row)
    check("reopen_child_direction", row[1] == 2, row)
    check("reopen_child_disposition", row[2] == 2, row)
    # seq renders as a JSON STRING here, not a number -- an uncast SP variable passed to
    # JSON_OBJECT() does this throughout the codebase (sp_mf_workflow_begin_reversal's own
    # `continuation` write does the same), not a defect specific to this SP.
    check("reopen_child_continuation", json.loads(row[3]) == {"pos": "reverse", "seq": "1"}, row)
    check("reopen_child_terminal_reason", row[4] == "parent_compensation", row)
    check("reopen_child_next_attempt_at", str(row[5]) == "2026-03-01 09:00:11", row)
    check("reopen_child_fencing_bumped", row[6] == 2, row)

    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (child_a,))
    check("reopen_child_event_count", cur.fetchone()[0] == child_events_before + 1, "expected exactly 1 new child event")
    cur.execute("SELECT kind, actor, payload FROM tb_mf_workflow_event WHERE workflow_id=%s ORDER BY event_ts DESC LIMIT 1",
                (child_a,))
    row = cur.fetchone()
    check("reopen_child_event_kind", row[0] == "compensation_requested_by_parent", row)
    check("reopen_child_event_actor", row[1] == EXEC, row)
    payload = json.loads(row[2])
    check("reopen_child_event_payload",
          payload.get("parent_workflow_id") == parent_a.hex() and payload.get("parent_operation_seq") == 1, payload)

    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (parent_a,))
    check("reopen_parent_event_count", cur.fetchone()[0] == parent_events_before + 1, "expected exactly 1 new parent event")
    cur.execute("SELECT kind, payload FROM tb_mf_workflow_event WHERE workflow_id=%s ORDER BY event_ts DESC LIMIT 1",
                (parent_a,))
    row = cur.fetchone()
    check("reopen_parent_event_kind", row[0] == "compensation_requested", row)
    payload = json.loads(row[1])
    # seq renders as a JSON STRING (same uncast-SP-variable behavior as sp_mf_checkpoint_reverse_
    # request's existing compensation_requested event payload, e.g. its own 'seq', arg_seq).
    check("reopen_parent_event_payload",
          payload.get("seq") == "1" and payload.get("child_workflow_id") == child_a.hex(), payload)

    cur.execute("SELECT state, continuation FROM tb_mf_workflow WHERE workflow_id=%s", (parent_a,))
    row = cur.fetchone()
    check("reopen_parent_state_untouched", row[0] == 2, row)
    check("reopen_parent_continuation_untouched", json.loads(row[1]) == {"pos": "reverse"}, row)

    # Idempotent replay: no duplicate events, no mutation, on EITHER side.
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (parent_a,))
    parent_events_before2 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (child_a,))
    child_events_before2 = cur.fetchone()[0]
    cur.execute("SELECT state, continuation, fencing_token, terminal_reason FROM tb_mf_workflow WHERE workflow_id=%s",
                (child_a,))
    child_row_before2 = cur.fetchone()

    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (parent_a, EXEC, 701, 1, T(12)))
    check("reopen_idempotent_replay", r and r["outcome"] == "already_reopened" and r["child_state"] == 2, r)

    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (parent_a,))
    check("reopen_idempotent_no_parent_event", cur.fetchone()[0] == parent_events_before2, "expected zero new parent events")
    cur.execute("SELECT COUNT(*) FROM tb_mf_workflow_event WHERE workflow_id=%s", (child_a,))
    check("reopen_idempotent_no_child_event", cur.fetchone()[0] == child_events_before2, "expected zero new child events")
    cur.execute("SELECT state, continuation, fencing_token, terminal_reason FROM tb_mf_workflow WHERE workflow_id=%s",
                (child_a,))
    check("reopen_idempotent_child_unchanged", cur.fetchone() == child_row_before2, "expected child row unchanged")

    # failed(7): diagnostic, never a benign skip -- corruption evidence, no write.
    child_failed = make_child_row(state=7, event_ts=T(1))
    parent_failed = make_parent_call_checkpoint(child_failed, seq=1, fencing_token=702, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (parent_failed, EXEC, 702, 1, T(2)))
    check("reopen_failed_child_diagnosed", r and r["outcome"] == "child_state_inconsistent" and r["child_state"] == 7, r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (child_failed,))
    check("reopen_failed_child_untouched", cur.fetchone()[0] == 7, "expected child state unchanged")

    # Dual event-time skew: EITHER clock being ahead of the proposed event_ts must block, before
    # any mutation -- not just the parent's own.
    child_c = make_child_row(state=4, event_ts=T(5), checkpoint_seq=1)
    parent_c = make_parent_call_checkpoint(child_c, seq=1, fencing_token=703, parent_event_ts=T(20))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (parent_c, EXEC, 703, 1, T(10)))
    check("reopen_time_skew_parent_clock", r and r["outcome"] == "event_time_skew", r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (child_c,))
    check("reopen_time_skew_parent_clock_no_write", cur.fetchone()[0] == 4, "expected no mutation")

    child_d = make_child_row(state=4, event_ts=T(20), checkpoint_seq=1)
    parent_d = make_parent_call_checkpoint(child_d, seq=1, fencing_token=704, parent_event_ts=T(5))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (parent_d, EXEC, 704, 1, T(10)))
    check("reopen_time_skew_child_clock", r and r["outcome"] == "event_time_skew", r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (child_d,))
    check("reopen_time_skew_child_clock_no_write", cur.fetchone()[0] == 4, "expected no mutation")

    # Reverse order (NULL-safe: this SP's idempotent check is on CHILD state, not checkpoint
    # state, so -- unlike settle -- v_top_seq is not already guaranteed non-NULL by an earlier
    # check; this pins that the order check handles it directly).
    child_f1 = make_child_row(state=4, event_ts=T(1), checkpoint_seq=1)
    child_f2 = make_child_row(state=4, event_ts=T(1), checkpoint_seq=1)
    wf_f = os.urandom(16)
    all_workflow_ids.append(wf_f)
    tok_f = 712
    cur.execute(
        "INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
        "execution_direction, current_disposition, current_event_ts, "
        "fencing_token, lease_owner, lease_expires_at, next_attempt_at, current_operation_attempt, "
        "continuation, created_at, updated_at) "
        "VALUES (%s,%s,1,2,2,2,%s,%s,%s,%s,%s,0,'{\"pos\":\"reverse\"}',%s,%s)",
        (wf_f, SCRIPT, T(1), tok_f, EXEC, "2026-03-01 10:00:00.000000", T(1), T(1), T(1)))
    op_f1, op_f2 = os.urandom(16), os.urandom(16)
    cur.execute(
        "INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
        "schema_version, input_json, input_hash, call_kind, status, result_json, created_at, updated_at) "
        "VALUES (%s,1,%s,'child-f1',1,'{}','h',2,2,'{}',%s,%s),"
        "(%s,2,%s,'child-f2',1,'{}','h',2,2,'{}',%s,%s)",
        (wf_f, op_f1, T(1), T(1), wf_f, op_f2, T(1), T(1)))
    cur.execute(
        "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, payload, "
        "reversal_state, created_at, updated_at) VALUES "
        "(%s,1,'child-f1',%s,'{}',1,%s,%s),(%s,2,'child-f2',%s,'{}',1,%s,%s)",
        (wf_f, op_f1, T(1), T(1), wf_f, op_f2, T(1), T(1)))
    cur.execute(
        "INSERT INTO tb_mf_call (workflow_id, operation_seq, child_workflow_id, child_script_name, "
        "child_plan_version, child_content_hash, child_status, first_requested_at, created_at, updated_at) "
        "VALUES (%s,1,%s,'child-f1','1.0.0',%s,2,%s,%s,%s),"
        "(%s,2,%s,'child-f2','1.0.0',%s,2,%s,%s,%s)",
        (wf_f, child_f1, bytes.fromhex("01" + "e1" * 32), T(1), T(1), T(1),
         wf_f, child_f2, bytes.fromhex("01" + "e1" * 32), T(1), T(1), T(1)))

    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (wf_f, EXEC, tok_f, 1, T(2)))
    check("reopen_out_of_order", r and r["outcome"] == "out_of_order" and r["top_seq"] == 2, r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (child_f1,))
    check("reopen_out_of_order_no_write", cur.fetchone()[0] == 4, "expected no mutation on the wrong-seq child")
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (wf_f, EXEC, tok_f, 2, T(2)))
    check("reopen_top_seq_ok", r and r["outcome"] == "reopened", r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (child_f2,))
    check("reopen_top_seq_ok_child_state", cur.fetchone()[0] == 2, "expected child_f2 reopened")

    # Type guard: rejected outright for a participant checkpoint (structural, before any state
    # machine logic -- shared by both new SPs).
    cur.execute("UPDATE tb_mf_workflow SET state=2, execution_direction=2, current_disposition=2, "
                "lease_owner=%s, lease_expires_at=%s, fencing_token=88 WHERE workflow_id=%s",
                (EXEC, "2026-03-01 10:00:00.000000", wf9b))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_reopen", (wf9b, EXEC, 88, 1, T(6)))
    check("reopen_rejects_participant_checkpoint", r and r["outcome"] == "not_call_checkpoint", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (wf9b, EXEC, 88, 1, T(6)))
    check("settle_rejects_participant_checkpoint", r and r["outcome"] == "not_call_checkpoint", r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (wf9b,))
    check("reopen_settle_participant_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged (1)")

    # ================================================================
    # 11. sp_mf_checkpoint_reverse_child_settle -- verifies child state itself; never trusts the
    #     caller. Refuses reversing/blocked/completed/failed; accepts reversed/resolved_exception;
    #     descend/terminal mechanics unchanged from the retired sp_mf_checkpoint_reverse_noop.
    # ================================================================
    # state=1 (forward): structurally impossible for a real call checkpoint's child, but the check
    # must still be an EXPLICIT `IN (5,6)` positive requirement, not an enumerated-rejection list --
    # an earlier (4,7)-only rejection form let state=1 fall through unnoticed and would have settled
    # the parent's checkpoint as if the child had actually compensated (review-caught regression;
    # this test pins it directly).
    child_fwd = make_child_row(state=1, event_ts=T(1))
    parent_fwd = make_parent_call_checkpoint(child_fwd, seq=1, fencing_token=713, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_fwd, EXEC, 713, 1, T(2)))
    check("settle_refuses_forward", r and r["outcome"] == "child_not_compensated" and r["child_state"] == 1, r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (parent_fwd,))
    check("settle_refuses_forward_checkpoint_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged")
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (parent_fwd,))
    check("settle_refuses_forward_parent_untouched", cur.fetchone()[0] == 2, "expected parent still reversing(2), not settled")

    child_rev = make_child_row(state=2, event_ts=T(1))
    parent_rev = make_parent_call_checkpoint(child_rev, seq=1, fencing_token=705, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_rev, EXEC, 705, 1, T(2)))
    check("settle_refuses_reversing", r and r["outcome"] == "child_not_terminal" and r["child_state"] == 2, r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (parent_rev,))
    check("settle_refuses_reversing_checkpoint_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged")

    child_blocked = make_child_row(state=3, event_ts=T(1))
    parent_blocked = make_parent_call_checkpoint(child_blocked, seq=1, fencing_token=706, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_blocked, EXEC, 706, 1, T(2)))
    check("settle_refuses_blocked", r and r["outcome"] == "child_not_terminal" and r["child_state"] == 3, r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (parent_blocked,))
    check("settle_refuses_blocked_checkpoint_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged")

    child_still_completed = make_child_row(state=4, event_ts=T(1), checkpoint_seq=1)
    parent_stc = make_parent_call_checkpoint(child_still_completed, seq=1, fencing_token=707, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_stc, EXEC, 707, 1, T(2)))
    check("settle_refuses_completed", r and r["outcome"] == "child_not_compensated" and r["child_state"] == 4, r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (parent_stc,))
    check("settle_refuses_completed_checkpoint_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged")

    child_failed2 = make_child_row(state=7, event_ts=T(1))
    parent_f2 = make_parent_call_checkpoint(child_failed2, seq=1, fencing_token=708, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_f2, EXEC, 708, 1, T(2)))
    check("settle_refuses_failed", r and r["outcome"] == "child_not_compensated" and r["child_state"] == 7, r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (parent_f2,))
    check("settle_refuses_failed_checkpoint_untouched", cur.fetchone()[0] == 1, "expected reversal_state unchanged")

    child_rsd = make_child_row(state=5, event_ts=T(1))
    parent_rsd = make_parent_call_checkpoint(child_rsd, seq=1, fencing_token=709, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_rsd, EXEC, 709, 1, T(2)))
    check("settle_accepts_reversed", r and r["outcome"] == "reversed", r)
    cur.execute("SELECT reversal_state FROM tb_mf_workflow_checkpoint WHERE workflow_id=%s AND seq=1", (parent_rsd,))
    check("settle_accepts_reversed_checkpoint_state", cur.fetchone()[0] == 2, "expected reversal_state=2")
    cur.execute("SELECT state, lease_owner FROM tb_mf_workflow WHERE workflow_id=%s", (parent_rsd,))
    row = cur.fetchone()
    check("settle_accepts_reversed_parent_terminal", row == (5, None), row)
    cur.execute("SELECT kind, payload FROM tb_mf_workflow_event WHERE workflow_id=%s ORDER BY event_ts DESC LIMIT 1",
                (parent_rsd,))
    row = cur.fetchone()
    check("settle_accepts_reversed_event", row[0] == "compensation_settled", row)
    # Correlation fields (1c-design.md's own requirement): the settle event must carry
    # child_workflow_id + the child's terminal state, same as reopen's own event already does --
    # this was missing in the first cut (review-caught, fixed).
    payload = json.loads(row[1])
    check("settle_accepts_reversed_event_correlation",
          payload.get("child_workflow_id") == child_rsd.hex() and payload.get("child_state") == 5, payload)

    child_rex = make_child_row(state=6, event_ts=T(1))
    parent_rex = make_parent_call_checkpoint(child_rex, seq=1, fencing_token=710, parent_event_ts=T(1))
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (parent_rex, EXEC, 710, 1, T(2)))
    check("settle_accepts_resolved_exception", r and r["outcome"] == "reversed", r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (parent_rex,))
    check("settle_accepts_resolved_exception_parent_terminal", cur.fetchone()[0] == 5, "expected parent state=5")
    cur.execute("SELECT payload FROM tb_mf_workflow_event WHERE workflow_id=%s ORDER BY event_ts DESC LIMIT 1",
                (parent_rex,))
    payload = json.loads(cur.fetchone()[0])
    check("settle_accepts_resolved_exception_event_correlation",
          payload.get("child_workflow_id") == child_rex.hex() and payload.get("child_state") == 6, payload)

    # Descend/terminal mechanics: unchanged from the retired sp_mf_checkpoint_reverse_noop --
    # out-of-order rejection, then descend (stays reversing, more to go), then the final settle
    # reaches the parent's own reversed(5) terminal. Two call checkpoints, both children already
    # reversed(5) (settle's own precondition, tested above, is not what this scenario is pinning).
    child_e1 = make_child_row(state=5, event_ts=T(1))
    child_e2 = make_child_row(state=5, event_ts=T(1))
    wf_e = os.urandom(16)
    all_workflow_ids.append(wf_e)
    tok_e = 711
    cur.execute(
        "INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
        "execution_direction, current_disposition, current_event_ts, "
        "fencing_token, lease_owner, lease_expires_at, next_attempt_at, current_operation_attempt, "
        "continuation, created_at, updated_at) "
        "VALUES (%s,%s,1,2,2,2,%s,%s,%s,%s,%s,0,'{\"pos\":\"reverse\"}',%s,%s)",
        (wf_e, SCRIPT, T(1), tok_e, EXEC, "2026-03-01 10:00:00.000000", T(1), T(1), T(1)))
    op_e1, op_e2 = os.urandom(16), os.urandom(16)
    cur.execute(
        "INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, operation_name, "
        "schema_version, input_json, input_hash, call_kind, status, result_json, created_at, updated_at) "
        "VALUES (%s,1,%s,'child-e1',1,'{}','h',2,2,'{}',%s,%s),"
        "(%s,2,%s,'child-e2',1,'{}','h',2,2,'{}',%s,%s)",
        (wf_e, op_e1, T(1), T(1), wf_e, op_e2, T(1), T(1)))
    cur.execute(
        "INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, operation_id, payload, "
        "reversal_state, created_at, updated_at) VALUES "
        "(%s,1,'child-e1',%s,'{}',1,%s,%s),(%s,2,'child-e2',%s,'{}',1,%s,%s)",
        (wf_e, op_e1, T(1), T(1), wf_e, op_e2, T(1), T(1)))
    cur.execute(
        "INSERT INTO tb_mf_call (workflow_id, operation_seq, child_workflow_id, child_script_name, "
        "child_plan_version, child_content_hash, child_status, first_requested_at, created_at, updated_at) "
        "VALUES (%s,1,%s,'child-e1','1.0.0',%s,2,%s,%s,%s),"
        "(%s,2,%s,'child-e2','1.0.0',%s,2,%s,%s,%s)",
        (wf_e, child_e1, bytes.fromhex("01" + "e1" * 32), T(1), T(1), T(1),
         wf_e, child_e2, bytes.fromhex("01" + "e1" * 32), T(1), T(1), T(1)))

    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (wf_e, EXEC, 999999, 2, T(2)))
    check("settle_fence_lost", r and r["outcome"] == "fence_lost", r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (wf_e, EXEC, tok_e, 1, T(2)))
    check("settle_out_of_order", r and r["outcome"] == "out_of_order" and r["top_seq"] == 2, r)
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (wf_e, EXEC, tok_e, 2, T(2)))
    check("settle_descends", r and r["outcome"] == "reversing" and r["next_seq"] == 1, r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (wf_e,))
    check("settle_descends_parent_still_reversing", cur.fetchone()[0] == 2, "expected parent still state=2 (more to compensate)")
    cur.execute("SELECT payload FROM tb_mf_workflow_event WHERE workflow_id=%s ORDER BY event_ts DESC LIMIT 1", (wf_e,))
    payload = json.loads(cur.fetchone()[0])
    check("settle_descends_event_correlation",
          payload.get("child_workflow_id") == child_e2.hex() and payload.get("child_state") == 5, payload)
    _, r = call(cur, "sp_mf_checkpoint_reverse_child_settle", (wf_e, EXEC, tok_e, 1, T(3)))
    check("settle_final_terminal", r and r["outcome"] == "reversed", r)
    cur.execute("SELECT state FROM tb_mf_workflow WHERE workflow_id=%s", (wf_e,))
    check("settle_final_terminal_parent_state", cur.fetchone()[0] == 5, "expected parent state=5 (reversed)")

    # ================================================================
    # Cleanup.
    # ================================================================
    for t in ("tb_mf_call", "tb_mf_workflow_args", "tb_mf_workflow_plan", "tb_mf_workflow_checkpoint",
              "tb_mf_operation", "tb_mf_workflow_event", "tb_mf_workflow"):
        for w in all_workflow_ids:
            cur.execute(f"DELETE FROM {t} WHERE workflow_id = %s", (w,))
    conn.close()

    EXPECTED_CHECKS = 131
    total = passed + len(failures)
    if total != EXPECTED_CHECKS:
        failures.append(f"completeness_guard: ran {total} checks, expected {EXPECTED_CHECKS}")
    print(f"sp_call regression: {passed}/{total} passed (expected {EXPECTED_CHECKS})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
