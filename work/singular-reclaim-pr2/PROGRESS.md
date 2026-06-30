# Singular PR2 — expired-lease reclaim via resume

Resolves the blocker: *Singular 0.7.0 cannot reclaim expired working operations, so MF same-operation
crash recovery can remain pending forever.* Protocol: `singular/doc/singular-protocol.md` §4 (token
rotation fences, not wall-clock) + §6.6 (resume/reclaim grants a fresh attempt + rotated token on an
expired lease, handing over persisted context). Was deferred PR1→PR2; the resume SP literally said "reclaim
is PR2."

## DONE + verified against the live DB (port 34214, `singular` schema)

- **`db/procs/sp_singular_resume.sql`** — rewritten for PR2. New nullable reclaim params
  `(lease_owner, lease_meta, event_ts, lease_expires_at, lease_token)`. Behaviour:
  - `FOR UPDATE` lock (PR2 may mutate). Read-only resume (NULL token) preserves PR1 `active`/`terminal`/
    `not_found`.
  - **Terminal replay first** — recovery token is NOT validated there, so a malformed token can never block
    a legitimate replay (the reason PR1 dropped these inputs).
  - **WORKING + token supplied + expired** (`event_ts > lease_expires_at`) → **GRANT**: strict event-time
    monotonicity, append a CLAIMED recovery event (carries forward `item_meta`+`checkpoint`), **rotate
    `current_lease_token`** (fences the prior holder, §4), return
    `{"outcome":"granted","kind":"reclaim","lease_expires_at","recovery_attempt":N,"checkpoint":{…}}`.
    `recovery_attempt` = count of CLAIMED events (start is #1, first reclaim → 1); JSON number via
    `CAST(... AS SIGNED)`.
  - **WORKING + token + not-yet-expired** → `active` (never steal a live lease, §6.6).
- **`drift/packages/singular/tests/sql/sp_invariants_test.py`** — added `test_reclaim` (9 assertions):
  not_found / read-only-active / live-lease-no-steal / expired→granted (recovery_attempt 1, +1 history row,
  projection unchanged) / **old-token→token_stale (fenced)** / new-token→settled / terminal-replay-with-junk-
  token / recovery_attempt 1→2. Also fixed the existing dangling-head test for the 7-arg signature. **`just
  test-sql` → "sp-invariants: all pass".** (Regression-first: the pre-change baseline returned `active` on
  expired and a stale `complete` SETTLED — both now corrected.)

## Steps 3 + 5 — IMPLEMENTED + VERIFIED (full `just test` green)

Built + run locally with `DRIFT_TOOLCHAIN_ROOT=~/opt/drift/certified/current/toolchain` +
`DRIFT_PKG_ROOT=~/opt/drift/certified/current/pkgs` (the justfiles default to the EMPTY `…/libs` — the real
packages are in `…/pkgs`). **`singular just test` = 16 ok, 0 failed**: `live_gateway_test` (base/asan/memcheck)
compiles the new gateway API + runs `scenario_reclaim_expired` (#18); `sp-invariants` runs the reclaim
regression; malformed/uuid tests unaffected. The reviewer's two compile blockers (duplicate `_doc_int_req`,
unexported `RecoveryLease`) are fixed and confirmed by the green build.

**Step 3 — gateway (`src/gateway.drift`)** ✅ written:
- `RecoveryLease` struct + `ResumeOutcome::Granted(lease, recovery_attempt: Int, checkpoint_json: String)`.
- `resume(self, key, recovery: &Optional<RecoveryLease>)`: `None` → 5× `rpc.arg_null()`; `Some(r)` →
  validate token+expiry, owner=identity, `_meta_object_arg`, `_utc_to_db`×2, token; decode `granted` →
  `Granted(WorkLease(key, r.token, expiry), _doc_int_req(…"recovery_attempt"), _doc_object_text_req(…"checkpoint"))`.
- The `granted` decode reuses the gateway's **existing** `_doc_int_req` helper (`n.as_int()`) — no new
  helper added.

**Step 5 — e2e (`tests/e2e/live_gateway_test.drift`)** ✅ written: 5 read-only `resume(&key)` sites →
`resume(&key, &_no_recovery())` (the `Granted` variant is caught by their existing `default` arms — no
exhaustiveness break); added `scenario_reclaim_expired` (#18): start → expire → reclaim Granted (recovery_attempt
1, rotated token) → old-token complete = TokenStale → new-token complete = Settled → read-only terminal replay.

This makes the change LANDABLE: the SP's new arg count and the gateway's resume() call move together.

## Step 4 — DONE + verified (participant-stub reclaim-on-PUT)

`microflows/participant-stub/src/app.drift`: the `start→Exists` (same input) branch now **reclaims via
`resume(key, RecoveryLease)`** instead of reporting 202 — Terminal→replay, **Active→202 (live lease never
stolen)**, **Granted→rerun idempotently + complete under the rotated lease→200**, NotFound→500. Added a
per-op **in-memory idempotency store** (the bookkeeper-ledger analog) so a reclaim rerun is **REPLAYED**
(no re-execution → `exec_count` stays 1), a **`crash_after_commit` fault** (commit + hold lease Working +
return 202 without completing), and an env-tunable **lease TTL** (`MICROFLOWS_STUB_LEASE_TTL_SECONDS`) for
fast expiry tests. **Verified:** stub builds (DRIFT_PKG_ROOT=…/pkgs) and `tests/http/conformance.py` is
**7/7** against the live DB, incl. `crash_after_commit_reclaim_on_put` (crash→expire→re-PUT→reclaim→complete,
exactly-once `exec_count==1`).

This is the microflows **reference** participant recovery; the bookkeeper applies the same pattern. Phase 7
case [12] is then unblocked end-to-end by this + the spec'd uflowsd pending→re-dispatch
(`work/uflowsd-pending-redispatch/`).

### Files (step 4)
- `microflows/participant-stub/src/app.drift` (idempotency store, crash fault, resume-reclaim branch, TTL env)
- `microflows/participant-stub/tests/http/conformance.py` (`crash_after_commit_reclaim_on_put` + short-TTL spawn)

## Files touched (all verified by `just test` green)
- `singular/db/procs/sp_singular_resume.sql` (reclaim rewrite)
- `singular/drift/packages/singular/src/gateway.drift` (RecoveryLease + export, ResumeOutcome::Granted, resume signature/impl)
- `singular/drift/packages/singular/src/lib.drift` (RecoveryLease re-export + type alias)
- `singular/drift/packages/singular/tests/e2e/live_gateway_test.drift` (5 read-only call sites + scenario_reclaim_expired #18 + header)
- `singular/drift/packages/singular/tests/sql/sp_invariants_test.py` (reclaim coverage + dangling-head arity)
