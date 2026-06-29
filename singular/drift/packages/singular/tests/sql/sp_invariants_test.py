#!/usr/bin/env python3
"""Raw-SQL / SP-level invariant regressions for Singular.

These exercise the stored procedures DIRECTLY (not through the gateway) — the only way to drive
cases the typed gateway cannot express (SQL NULL arguments, a deliberately corrupted backend state)
and to assert table state (row counts) is unchanged on rejection.

Concurrency-safe: every fixture row uses a PER-RUN nonce in its service_group, and cleanup deletes
ONLY this run's exact service_groups — so parallel certification runs against the shared instance
never collide with or delete each other's fixtures.

Covered:
  - 0.5 caller-time contract: start/extend_lease with lease_expires_at <= event_time SIGNAL
    'SingularInvalidLeaseExpiry' (errno 30003); extend with a non-monotonic event_time (<= the item's
    last) SIGNALs 'SingularEventTimeConflict' (errno 30002); a NULL event_time SIGNALs
    'SingularEventTimeInvalid'. All leave projection + history COUNT and the stored expiry unchanged.
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

HOST = os.environ.get("DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("DB_PORT", "34214"))
USER = os.environ.get("DB_USER", "root")
PWD = os.environ.get("MDB_ROOT_PWD", "rootpw")
DB = "singular"

CORRUPTION_ERRNO = 30001          # MYSQL_ERRNO on SingularHeadHistoryMissing (SQLSTATE '45001');
                                  # kept < 2^15 so it reads identically across clients (drift + pymysql)
OWNER = bytes(16)                 # 16-byte lease_owner (binary(16))
OWNER2 = bytes([0xb2]) * 16       # a SECOND owner (the reclaiming worker)
TOKEN = bytes(range(16))          # valid 16-byte capability token
TOKEN2 = bytes(range(16, 32))     # a SECOND token (rotated in on reclaim)
KEY = b"\x01"                     # 1-byte idempotency key (valid: 1..32 bytes)

EXPIRY_ERRNO = 30003              # MYSQL_ERRNO on SingularInvalidLeaseExpiry
CONFLICT_ERRNO = 30002            # MYSQL_ERRNO on SingularEventTimeConflict
# 0.5: caller-supplied absolute times (datetime(6) SP args). EV < EV2, and EXP is 30s after EV (> EV).
EV = "2026-01-01 00:00:00.000000"   # a caller event time
EXP = "2026-01-01 00:00:30.000000"  # a lease deadline strictly after EV
EV2 = "2026-01-01 00:00:01.000000"  # a strictly-later event time (settle/extend after a start at EV)

NONCE = uuid.uuid4().hex[:12]     # per-run isolation
SG = {name: f"sptest-{NONCE}-{name}"
      for name in ("start", "extend", "settle", "dangling", "meta", "resp", "owner", "ckpair", "reclaim")}

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


def call_result(cur, proc, args):
    """callproc + return the parsed JSON `result` document (first column of the first result set)."""
    cur.callproc(proc, args)
    row = cur.fetchone()
    while cur.nextset():
        pass
    if row is None or row[0] is None:
        return None
    return json.loads(row[0])


def _expect_outcome(label, doc, want):
    got = doc.get("outcome") if isinstance(doc, dict) else None
    if got == want:
        _ok(f"{label}: outcome={want}")
        return True
    _fail(f"{label}: outcome={got!r} (wanted {want!r}); doc={doc!r}")
    return False


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


def test_start_expiry(cur):
    # 0.5: start takes caller event_time + absolute lease_expires_at. lease_expires_at <= event_time ->
    # SingularInvalidLeaseExpiry (errno 30003); a NULL event_time -> SingularEventTimeInvalid. No row.
    sg = SG["start"]
    before = counts(cur, sg)
    bad_exp = [("equal", EV, EV), ("earlier", EV, "2025-12-31 23:59:59.000000"), ("expiry-NULL", EV, None)]
    for label, ev, exp in bad_exp:
        expect_signal(cur, f"start expiry={label}", "sp_singular_start",
                      (sg, KEY, "{}", OWNER, "{}", ev, exp, TOKEN), "SingularInvalidLeaseExpiry", EXPIRY_ERRNO)
    expect_signal(cur, "start event_ts=NULL", "sp_singular_start",
                  (sg, KEY, "{}", OWNER, "{}", None, EXP, TOKEN), "SingularEventTimeInvalid")
    after = counts(cur, sg)
    if after == before == (0, 0):
        _ok("start: rejected invalid expiry / NULL event_ts created no projection/history row")
    else:
        _fail(f"start: row counts changed {before} -> {after} (expected (0,0))")


def test_extend_expiry_and_monotonicity(cur):
    # 0.5: extend takes caller event_time + absolute lease_expires_at. A non-monotonic event_time
    # (<= the item's last) -> SingularEventTimeConflict (30002); lease_expires_at <= event_time ->
    # SingularInvalidLeaseExpiry (30003). Neither mutates history nor changes the stored expiry.
    sg = SG["extend"]
    call(cur, "sp_singular_start", (sg, KEY, "{}", OWNER, "{}", EV, EXP, TOKEN))   # live lease at EV
    before = counts(cur, sg)
    cur.execute("SELECT lease_expires_at FROM tb_singular_work_item_history WHERE service_group=%s", (sg,))
    exp_before = cur.fetchone()[0]
    # bad expiry on a monotonic event (EV2 > EV): lease_expires_at <= event_time
    for label, exp in (("equal", EV2), ("earlier", EV)):
        expect_signal(cur, f"extend expiry={label}", "sp_singular_extend_lease",
                      (sg, KEY, OWNER, TOKEN, EV2, exp), "SingularInvalidLeaseExpiry", EXPIRY_ERRNO)
    # non-monotonic event_time (== current EV): EventTimeConflict regardless of the expiry arg
    expect_signal(cur, "extend event<=last", "sp_singular_extend_lease",
                  (sg, KEY, OWNER, TOKEN, EV, EXP), "SingularEventTimeConflict", CONFLICT_ERRNO)
    after = counts(cur, sg)
    cur.execute("SELECT lease_expires_at FROM tb_singular_work_item_history WHERE service_group=%s", (sg,))
    exp_after = cur.fetchone()[0]
    if after == before == (1, 1) and exp_after == exp_before:
        _ok("extend: rejected expiry/monotonicity violations mutated no history and left expiry unchanged")
    else:
        _fail(f"extend: state changed counts {before}->{after}, expiry {exp_before}->{exp_after}")


def test_settle_monotonicity(cur):
    # 0.5: complete AND fail require event_time strictly after the item's last event (the claim at EV).
    # event_time <= current -> SingularEventTimeConflict (30002), no settle. A strictly-later complete
    # then settles. Pins the conflict at the SP boundary for the terminal transitions (not just extend).
    sg = SG["settle"]
    call(cur, "sp_singular_start", (sg, KEY, "{}", OWNER, "{}", EV, EXP, TOKEN))   # claim at EV
    earlier = "2025-12-31 23:59:59.000000"
    for label, ev in (("equal", EV), ("earlier", earlier)):
        expect_signal(cur, f"complete event={label}", "sp_singular_complete",
                      (sg, KEY, OWNER, TOKEN, ev, "{}"), "SingularEventTimeConflict", CONFLICT_ERRNO)
        expect_signal(cur, f"fail event={label}", "sp_singular_fail",
                      (sg, KEY, OWNER, TOKEN, ev, "{}"), "SingularEventTimeConflict", CONFLICT_ERRNO)
    # the rejected settles left it WORKING; a strictly-later complete settles it DONE.
    try:
        call(cur, "sp_singular_complete", (sg, KEY, OWNER, TOKEN, EV2, '{"r":"ok"}'))
    except pymysql.MySQLError as e:
        _fail(f"settle monotonicity: monotonic complete unexpectedly raised {e!r}"); return
    cur.execute("SELECT status FROM tb_singular_work_item_history "
                "WHERE service_group=%s AND event_type=20", (sg,))   # COMPLETED event
    row = cur.fetchone()
    if row is not None and row[0] == 2:   # DONE
        _ok("settle monotonicity: complete/fail reject event<=last (30002); a monotonic complete settles")
    else:
        _fail(f"settle monotonicity: expected a DONE terminal after the monotonic complete, got {row!r}")


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
    expect_signal(cur, "resume dangling-head", "sp_singular_resume", (sg, KEY, None, None, None, None, None), want, errno)
    expect_signal(cur, "complete dangling-head", "sp_singular_complete", (sg, KEY, OWNER, TOKEN, EV2, "{}"), want, errno)
    expect_signal(cur, "fail dangling-head", "sp_singular_fail", (sg, KEY, OWNER, TOKEN, EV2, "{}"), want, errno)
    expect_signal(cur, "extend dangling-head", "sp_singular_extend_lease", (sg, KEY, OWNER, TOKEN, EV2, EXP), want, errno)


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
                      (sg, KEY, meta, OWNER, "{}", EV, EXP, TOKEN), "SingularItemMetaInvalid")
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
            call(cur, "sp_singular_start", (sg, k, meta, OWNER, "{}", EV, EXP, TOKEN))
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
    call(cur, "sp_singular_start", (sg, k, "{}", OWNER, "{}", EV, EXP, TOKEN))   # live lease at EV
    rejects = [("NULL", None), ("malformed", "{"), ("json-null", "null"), ("array", "[1,2]"), ("scalar", "5")]
    for label, resp in rejects:
        expect_signal(cur, f"complete response={label}", "sp_singular_complete",
                      (sg, k, OWNER, TOKEN, EV2, resp), "SingularResponseInvalid")
    # Still WORKING (rejected completes never settled it), then a nested-array object settles it at EV2.
    try:
        call(cur, "sp_singular_complete", (sg, k, OWNER, TOKEN, EV2, '{"items":[1,2]}'))
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
        ("start",        "sp_singular_start",        lambda o: (sg, KEY, "{}", o, "{}", EV, EXP, TOKEN)),
        ("complete",     "sp_singular_complete",     lambda o: (sg, KEY, o, TOKEN, EV2, "{}")),
        ("fail",         "sp_singular_fail",         lambda o: (sg, KEY, o, TOKEN, EV2, "{}")),
        ("extend_lease", "sp_singular_extend_lease", lambda o: (sg, KEY, o, TOKEN, EV2, EXP)),
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


def test_reclaim(cur):
    # PR2 expired-lease RECLAIM via resume — the coordinator crash-recovery contract (§4/§6.6). A worker
    # crashes after committing its side effect but before complete(); after lease expiry a NEW worker
    # resumes with a fresh recovery lease, is GRANTED a new attempt + ROTATED token (fencing the dead
    # holder), and drives the SAME idempotency_key to terminal. Read-only resume, live-lease no-steal,
    # recovery_attempt increment, and terminal-replay-not-blocked-by-token are all pinned here.
    sg = SG["reclaim"]
    K = b"\x01"
    # times: start live at EV..EXP (00:00:00..00:00:30); expiry is at/after EXP.
    EV_LIVE = "2026-01-01 00:00:01.000000"   # < EXP -> a still-live lease
    R1 = "2026-01-01 00:01:00.000000"        # > EXP -> expired; the first reclaim claim time
    R1X = "2026-01-01 00:01:30.000000"       # the reclaim's new deadline (> R1)
    C_NEW = "2026-01-01 00:01:10.000000"     # new-token complete (> R1, the current head)
    T_REPLAY = "2026-01-01 00:02:00.000000"

    # NOT_FOUND: a reclaim on a never-started key is not_found (not a grant).
    _expect_outcome("reclaim missing-key", call_result(cur, "sp_singular_resume",
                    (sg, b"\x09", OWNER2, "{}", R1, R1X, TOKEN2)), "not_found")

    call(cur, "sp_singular_start", (sg, K, '{"id":"req-1"}', OWNER, "{}", EV, EXP, TOKEN))  # live at EV

    # read-only resume (all reclaim params NULL) is preserved: ACTIVE (never grants).
    d = call_result(cur, "sp_singular_resume", (sg, K, None, None, None, None, None))
    if _expect_outcome("reclaim read-only", d, "active") and d.get("lease_expires_at") != EXP:
        _fail(f"reclaim read-only: lease_expires_at={d.get('lease_expires_at')!r} (wanted {EXP})")

    # live-lease reclaim attempt (claim time < EXP) -> ACTIVE, never steal a live lease.
    _expect_outcome("reclaim live-lease no-steal", call_result(cur, "sp_singular_resume",
                    (sg, K, OWNER2, "{}", EV_LIVE, "2026-01-01 00:00:40.000000", TOKEN2)), "active")

    # EXPIRED reclaim -> GRANTED, kind=reclaim, recovery_attempt 1, checkpoint handed over.
    before = counts(cur, sg)
    g = call_result(cur, "sp_singular_resume", (sg, K, OWNER2, "{}", R1, R1X, TOKEN2))
    okg = _expect_outcome("reclaim expired -> granted", g, "granted")
    if okg and (g.get("kind") != "reclaim" or g.get("recovery_attempt") != 1
                or g.get("lease_expires_at") != R1X or not isinstance(g.get("checkpoint"), dict)):
        _fail(f"reclaim granted fields wrong: {g!r}")
        okg = False
    after = counts(cur, sg)
    if after != (before[0], before[1] + 1):
        _fail(f"reclaim: projection/history counts {before} -> {after} (expected +1 history row, same projection)")
    elif okg:
        _ok("reclaim expired: granted reclaim (recovery_attempt 1, +1 CLAIMED event, projection unchanged)")

    # OLD token is now fenced: complete() under TOKEN -> token_stale (the dead worker can never publish).
    _expect_outcome("reclaim old-token fenced", call_result(cur, "sp_singular_complete",
                    (sg, K, OWNER, TOKEN, "2026-01-01 00:01:05.000000", '{"receipt":"old"}')), "token_stale")

    # NEW token drives the SAME key to terminal -> settled done.
    s = call_result(cur, "sp_singular_complete", (sg, K, OWNER2, TOKEN2, C_NEW, '{"receipt":"new"}'))
    if _expect_outcome("reclaim new-token settles", s, "settled") and s.get("payload") != {"receipt": "new"}:
        _fail(f"reclaim settle payload={s.get('payload')!r} (wanted receipt=new)")

    # Terminal replay is NOT blocked by a (deliberately malformed, 8-byte) recovery token: resume returns
    # the terminal document — the token is only validated on the reclaim-eligible (working) path.
    _expect_outcome("reclaim terminal replay (junk token ignored)", call_result(cur, "sp_singular_resume",
                    (sg, K, OWNER2, "{}", T_REPLAY, "2026-01-01 00:02:30.000000", bytes(8))), "terminal")

    # recovery_attempt increments across successive reclaims on a fresh key (start=CLAIMED#1 -> 1 -> 2).
    K2 = b"\x02"
    call(cur, "sp_singular_start", (sg, K2, "{}", OWNER, "{}", EV, EXP, TOKEN))
    g1 = call_result(cur, "sp_singular_resume", (sg, K2, OWNER2, "{}", R1, R1X, TOKEN2))
    g2 = call_result(cur, "sp_singular_resume", (sg, K2, OWNER, "{}", T_REPLAY, "2026-01-01 00:02:30.000000", TOKEN))
    if g1.get("recovery_attempt") == 1 and g2.get("recovery_attempt") == 2:
        _ok("reclaim: recovery_attempt increments 1 -> 2 across successive reclaims")
    else:
        _fail(f"reclaim recovery_attempt: {g1.get('recovery_attempt')!r}, {g2.get('recovery_attempt')!r} (wanted 1, 2)")


def main():
    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD, database=DB, autocommit=True)
    cur = conn.cursor()
    try:
        cleanup(cur)
        print(f"sp-invariants (nonce {NONCE}): lease-expiry contract (start)")
        test_start_expiry(cur)
        print("sp-invariants: lease-expiry + event monotonicity (extend)")
        test_extend_expiry_and_monotonicity(cur)
        print("sp-invariants: event monotonicity (complete + fail)")
        test_settle_monotonicity(cur)
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
        print("sp-invariants: expired-lease RECLAIM via resume (PR2 crash-recovery contract)")
        test_reclaim(cur)
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
