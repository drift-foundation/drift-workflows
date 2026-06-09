#!/usr/bin/env python3
"""Raw-SQL / SP-level invariant regressions for Singular.

These exercise the stored procedures DIRECTLY (not through the gateway) — the only way to drive
cases the typed gateway cannot express (SQL NULL arguments, a deliberately corrupted backend state)
and to assert table state (row counts) is unchanged on rejection.

Concurrency-safe: every fixture row uses a PER-RUN nonce in its service_group, and cleanup deletes
ONLY this run's exact service_groups — so parallel certification runs against the shared instance
never collide with or delete each other's fixtures.

Covered:
  - Lease-timeout defense-in-depth: start/extend_lease with SQL NULL, 0, and negative timeout
    SIGNAL 'SingularLeaseTimeoutInvalid' and leave projection + history COUNT unchanged.
  - Dangling head-history (corruption): a projection row whose current_event_ts has no matching
    history row makes inspect/resume/complete/fail/extend_lease SIGNAL 'SingularHeadHistoryMissing'
    with MYSQL_ERRNO 30001 (the structured code the gateway maps to BackendResponseInvalid).
  - Control: a missing projection makes inspect return a NotFound document (no error) — so NotFound
    and corruption are genuinely distinguished.
  - JSON object contract on SP inputs: item_meta (start) and the terminal payload (complete) must be
    a non-NULL JSON OBJECT. SQL NULL / malformed JSON / JSON null / array / scalar SIGNAL
    SingularItemMetaInvalid / SingularResponseInvalid (and create/settle nothing); `{}`, populated
    objects, and objects with a NESTED array are accepted.
  - lease_owner input contract: every owner-taking SP (start/complete/fail/extend_lease) takes
    varbinary(16) and SIGNALs SingularLeaseOwnerInvalid on a NULL or non-16-byte owner (no row).
  - event/status pair: the schema CHECK ck_singular_history_event_status rejects a mismatched pair
    (e.g. COMPLETED+WORKING) and accepts a valid one (COMPLETED+DONE).

Run via `just test-sql` (mariachi venv pymysql). Connects to the same mdb114-a / `singular` schema
the e2e gate uses (root/rootpw by default; override with MDB_ROOT_PWD).
"""

import json
import os
import sys
import uuid

import pymysql

HOST = "127.0.0.1"
PORT = 34114
USER = "root"
PWD = os.environ.get("MDB_ROOT_PWD", "rootpw")
DB = "singular"

CORRUPTION_ERRNO = 30001          # MYSQL_ERRNO on SingularHeadHistoryMissing (SQLSTATE '45001');
                                  # kept < 2^15 so it reads identically across clients (drift + pymysql)
OWNER = bytes(16)                 # 16-byte lease_owner (binary(16))
TOKEN = bytes(range(16))          # valid 16-byte capability token
KEY = b"\x01"                     # 1-byte idempotency key (valid: 1..32 bytes)

NONCE = uuid.uuid4().hex[:12]     # per-run isolation
SG = {name: f"sptest-{NONCE}-{name}"
      for name in ("start", "extend", "dangling", "meta", "resp", "owner", "ckpair")}

_failures = []


def _fail(msg):
    _failures.append(msg)
    print(f"  FAIL: {msg}")


def _ok(msg):
    print(f"  ok:   {msg}")


def counts(cur, sg):
    cur.execute("SELECT COUNT(*) FROM tb_singular_work_item WHERE service_group=%s", (sg,))
    items = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tb_singular_work_item_history WHERE service_group=%s", (sg,))
    hist = cur.fetchone()[0]
    return (items, hist)


def call(cur, proc, args):
    """callproc + drain any result sets."""
    cur.callproc(proc, args)
    while True:
        try:
            cur.fetchall()
        except Exception:
            pass
        if not cur.nextset():
            break


def expect_signal(cur, label, proc, args, want_msg, want_errno=None):
    try:
        call(cur, proc, args)
    except pymysql.MySQLError as e:
        errno = e.args[0] if e.args else None
        msg = str(e)
        ok = want_msg in msg and (want_errno is None or errno == want_errno)
        if ok:
            suffix = f" (errno {errno})" if want_errno is not None else ""
            _ok(f"{label}: SIGNAL {want_msg}{suffix}")
        else:
            _fail(f"{label}: raised errno={errno} msg={msg!r}; wanted {want_msg!r}"
                  + (f" errno {want_errno}" if want_errno is not None else ""))
        return
    _fail(f"{label}: expected SIGNAL {want_msg}, but the call succeeded")


def cleanup(cur):
    sgs = list(SG.values())
    placeholders = ",".join(["%s"] * len(sgs))
    cur.execute(f"DELETE FROM tb_singular_work_item_history WHERE service_group IN ({placeholders})", sgs)
    cur.execute(f"DELETE FROM tb_singular_work_item WHERE service_group IN ({placeholders})", sgs)


def test_start_timeout(cur):
    sg = SG["start"]
    before = counts(cur, sg)
    for label, timeout in (("NULL", None), ("zero", 0), ("negative", -5)):
        expect_signal(cur, f"start timeout={label}", "sp_singular_start",
                      (sg, KEY, "{}", OWNER, "{}", timeout, TOKEN), "SingularLeaseTimeoutInvalid")
    after = counts(cur, sg)
    if after == before == (0, 0):
        _ok("start: rejected timeouts created no projection/history row")
    else:
        _fail(f"start: row counts changed {before} -> {after} (expected (0,0))")


def test_extend_timeout(cur):
    sg = SG["extend"]
    call(cur, "sp_singular_start", (sg, KEY, "{}", OWNER, "{}", 30, TOKEN))   # valid live lease
    before = counts(cur, sg)
    cur.execute("SELECT lease_expires_at FROM tb_singular_work_item_history WHERE service_group=%s", (sg,))
    exp_before = cur.fetchone()[0]
    for label, timeout in (("NULL", None), ("zero", 0), ("negative", -1)):
        expect_signal(cur, f"extend timeout={label}", "sp_singular_extend_lease",
                      (sg, KEY, OWNER, TOKEN, timeout), "SingularLeaseTimeoutInvalid")
    after = counts(cur, sg)
    cur.execute("SELECT lease_expires_at FROM tb_singular_work_item_history WHERE service_group=%s", (sg,))
    exp_after = cur.fetchone()[0]
    if after == before == (1, 1) and exp_after == exp_before:
        _ok("extend: rejected timeouts mutated no history row and left expiry unchanged")
    else:
        _fail(f"extend: state changed counts {before}->{after}, expiry {exp_before}->{exp_after}")


def test_dangling_head(cur):
    sg = SG["dangling"]
    # Projection row whose current_event_ts points at a NON-EXISTENT history event.
    cur.execute(
        "INSERT INTO tb_singular_work_item "
        "(service_group, idempotency_key, current_event_ts, checkpoint_payload, current_lease_token, terminal_lease_token, created_at, updated_at) "
        "VALUES (%s, %s, '2020-01-01 00:00:00.000000', '{}', NULL, NULL, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
        (sg, KEY))
    want, errno = "SingularHeadHistoryMissing", CORRUPTION_ERRNO
    # All five SPs must surface the dangling head as corruption (errno 30001), not NotFound/NULL.
    expect_signal(cur, "inspect dangling-head", "sp_singular_inspect", (sg, KEY), want, errno)
    expect_signal(cur, "resume dangling-head", "sp_singular_resume", (sg, KEY), want, errno)
    expect_signal(cur, "complete dangling-head", "sp_singular_complete", (sg, KEY, OWNER, TOKEN, "{}"), want, errno)
    expect_signal(cur, "fail dangling-head", "sp_singular_fail", (sg, KEY, OWNER, TOKEN, "{}"), want, errno)
    expect_signal(cur, "extend dangling-head", "sp_singular_extend_lease", (sg, KEY, OWNER, TOKEN, 30), want, errno)


def test_missing_projection_is_notfound(cur):
    # Control: a missing projection is NotFound ({"outcome":"not_found"} document), NOT corruption.
    sg = SG["dangling"]  # same group, a DIFFERENT (never-inserted) key
    missing_key = b"\x02"
    try:
        cur.callproc("sp_singular_inspect", (sg, missing_key))
        row = cur.fetchone()
        while cur.nextset():
            pass
    except pymysql.MySQLError as e:
        _fail(f"missing-projection: inspect raised {e!r} (expected a NotFound document, not an error)")
        return
    doc = json.loads(row[0]) if row is not None and row[0] is not None else None
    if isinstance(doc, dict) and doc.get("outcome") == "not_found":
        _ok('missing-projection: inspect returned {"outcome":"not_found"}, no error')
    else:
        _fail(f"missing-projection: expected a not_found document, got {row!r}")


def test_start_item_meta_contract(cur):
    # JSON object contract on a PERSISTED SP input: item_meta must be a non-NULL JSON OBJECT.
    sg = SG["meta"]
    before = counts(cur, sg)
    rejects = [("NULL", None), ("malformed", "{"), ("json-null", "null"), ("array", "[1,2]"), ("scalar", "5")]
    for label, meta in rejects:
        expect_signal(cur, f"start item_meta={label}", "sp_singular_start",
                      (sg, KEY, meta, OWNER, "{}", 30, TOKEN), "SingularItemMetaInvalid")
    mid = counts(cur, sg)
    if mid == before == (0, 0):
        _ok("start item_meta: rejected NULL/malformed/json-null/array/scalar created no row")
    else:
        _fail(f"start item_meta: rejected metas changed counts {before} -> {mid} (expected (0,0))")
    # ACCEPT: {} (empty document), a populated object, and an object with a NESTED array.
    accepts = [(b"\x10", "{}"), (b"\x11", '{"a":1}'), (b"\x12", '{"items":[1,2]}')]
    ok = True
    for k, meta in accepts:
        try:
            call(cur, "sp_singular_start", (sg, k, meta, OWNER, "{}", 30, TOKEN))
        except pymysql.MySQLError as e:
            ok = False
            _fail(f"start item_meta accept {meta!r}: unexpected SIGNAL {e!r}")
    after = counts(cur, sg)
    if ok and after == (3, 3):
        _ok("start item_meta: accepted {} / populated / nested-array objects (3 rows)")
    else:
        _fail(f"start item_meta: accept path wrong (ok={ok}, counts={after}, expected (3,3))")


def test_complete_response_contract(cur):
    # JSON object contract on the terminal payload SP input: arg_response must be a JSON OBJECT.
    sg = SG["resp"]
    k = b"\x01"
    call(cur, "sp_singular_start", (sg, k, "{}", OWNER, "{}", 30, TOKEN))   # live lease
    rejects = [("NULL", None), ("malformed", "{"), ("json-null", "null"), ("array", "[1,2]"), ("scalar", "5")]
    for label, resp in rejects:
        expect_signal(cur, f"complete response={label}", "sp_singular_complete",
                      (sg, k, OWNER, TOKEN, resp), "SingularResponseInvalid")
    # Still WORKING (rejected completes never settled it), then a nested-array object settles it.
    try:
        call(cur, "sp_singular_complete", (sg, k, OWNER, TOKEN, '{"items":[1,2]}'))
        _ok("complete response: rejected non-objects, accepted nested-array object")
    except pymysql.MySQLError as e:
        _fail(f"complete response accept: unexpected SIGNAL {e!r}")


def test_owner_input_contract(cur):
    # lease_owner is a PERSISTED non-null 16-byte value. Every owner-taking SP takes varbinary(16)
    # and rejects NULL / non-16-byte owners (so a short value is not silently zero-padded), before
    # any write.
    sg = SG["owner"]
    before = counts(cur, sg)
    cases = [
        ("start",        "sp_singular_start",        lambda o: (sg, KEY, "{}", o, "{}", 30, TOKEN)),
        ("complete",     "sp_singular_complete",     lambda o: (sg, KEY, o, TOKEN, "{}")),
        ("fail",         "sp_singular_fail",         lambda o: (sg, KEY, o, TOKEN, "{}")),
        ("extend_lease", "sp_singular_extend_lease", lambda o: (sg, KEY, o, TOKEN, 30)),
    ]
    for name, sp, mk in cases:
        for label, owner in (("NULL", None), ("short", bytes(8))):
            expect_signal(cur, f"{name} owner={label}", sp, mk(owner), "SingularLeaseOwnerInvalid")
    after = counts(cur, sg)
    if after == before == (0, 0):
        _ok("owner-input: NULL/short owner rejected on start/complete/fail/extend, no row")
    else:
        _fail(f"owner-input: rejected owners changed counts {before} -> {after} (expected (0,0))")


def test_event_status_check(cur):
    # The schema CHECK ck_singular_history_event_status makes a mismatched (event_type, status) pair
    # unrepresentable; the gateway also cross-checks on decode (pinned by the malformed fixture).
    sg = SG["ckpair"]
    cols = ("service_group, idempotency_key, event_ts, item_meta, lease_owner, lease_meta, "
            "lease_expires_at, event_type, status, event_payload, lease_token, checkpoint_payload")
    try:
        cur.execute(
            f"INSERT INTO tb_singular_work_item_history ({cols}) "
            "VALUES (%s, %s, '2020-01-01 00:00:00.000000', '{}', %s, '{}', "
            "'2020-01-01 00:01:00.000000', 20, 1, '{}', NULL, '{}')",   # COMPLETED(20) + WORKING(1)
            (sg, b"\x01", OWNER))
        _fail("event/status CHECK: a COMPLETED+WORKING row was accepted (CHECK missing)")
    except pymysql.MySQLError:
        _ok("event/status CHECK: COMPLETED+WORKING rejected by schema CHECK")
    try:
        cur.execute(
            f"INSERT INTO tb_singular_work_item_history ({cols}) "
            "VALUES (%s, %s, '2020-01-01 00:00:00.000000', '{}', %s, '{}', "
            "NULL, 20, 2, '{}', NULL, '{}')",                           # COMPLETED(20) + DONE(2)
            (sg, b"\x02", OWNER))
        _ok("event/status CHECK: COMPLETED+DONE accepted")
    except pymysql.MySQLError as e:
        _fail(f"event/status CHECK: valid COMPLETED+DONE rejected: {e!r}")


def main():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD, database=DB, autocommit=True)
    cur = conn.cursor()
    try:
        cleanup(cur)
        print(f"sp-invariants (nonce {NONCE}): lease-timeout (start)")
        test_start_timeout(cur)
        print("sp-invariants: lease-timeout (extend)")
        test_extend_timeout(cur)
        print("sp-invariants: dangling head-history (corruption, errno 30001)")
        test_dangling_head(cur)
        print("sp-invariants: missing projection -> NotFound (control)")
        test_missing_projection_is_notfound(cur)
        print("sp-invariants: item_meta JSON object contract (start)")
        test_start_item_meta_contract(cur)
        print("sp-invariants: terminal-payload JSON object contract (complete)")
        test_complete_response_contract(cur)
        print("sp-invariants: lease_owner input contract (start/complete/fail/extend)")
        test_owner_input_contract(cur)
        print("sp-invariants: event/status pair CHECK (history)")
        test_event_status_check(cur)
    finally:
        # Always remove this run's exact fixtures, even if a test raised unexpectedly.
        try:
            cleanup(cur)
        finally:
            conn.close()

    if _failures:
        print(f"\nsp-invariants: {len(_failures)} FAILED")
        sys.exit(1)
    print("\nsp-invariants: all pass")


if __name__ == "__main__":
    main()
