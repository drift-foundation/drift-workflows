# uflowsd pending→re-dispatch recovery — PROGRESS

## Status

**IMPLEMENTED + verified (all gates green).** `SPEC.md` holds the design (Option A: re-dispatch via
byte-identical PUT; GET stays a pure read). The Phase-7 case-[12] gap is closed end-to-end: on a *recovered*
operation whose participant committed and crashed before `complete`, uflowsd escalates a confirmed GET-202 via
a durable, fenced re-dispatch timer and re-PUTs, so the participant reclaims its expired Singular lease and
completes — exactly once.

### Verified
- **SP regression** (`db-tests/sp_operation_test.py`): **148/148** — forward + reverse pending-defer
  (defer/redispatch boundary, epoch-anchored-once, fence_lost, all guardrails, atomic-defer no-partial-state,
  redispatch holds lease + re-arms, skew folds to no-append but still defers).
- **microflows package gate** (`just test`): 20/20 e2e (base/asan/memcheck) + 99/99 parser + 148/148 SP.
- **coordinator↔singular integration** (`just test`): **208/208**, incl. the new `c12_*` case — fresh
  crash-after-commit → pending; recovered GET 202 → escalate → re-PUT reclaim → **completed**; `exec_count`==1
  (effectively-once through reclaim); durable `redispatch_count` advanced; +1 re-dispatch PUT issued.

### App-team item (still open, non-blocking)
`pending_redispatch_after_ms` default **60000** is shipped + strictly validated + configurable. Confirm with
the app team it is ≥ the real participant Singular lease TTL + margin for production (per the original
instruction, proceeded with 60000 since no quick answer).

### Out of repo
The **bookkeeper** `harness/run_ledger_stress.py` case-[12] xfail→pass lives in the app team's repo (not here);
the reference participant-stub + the uflowsd integration `c12_*` case are the in-repo equivalents.

### Footgun hit + recorded
A Drift **`match` on an owned `var` consumes it** (even a non-binding `default` arm), so a second `match` reads
moved-from (empty) payloads — this crashed the recovered-dispatch settle until the detection match was switched
to a **borrow** (`match &eff_dr`). Saved to memory.

---
## Original spec-phase notes

## Decisions locked

- **Durable + fenced** escalation timer (parallel of the #2 reconcile-budget), not an in-memory `_reconcile`
  timer. Columns `redispatch_first_seen_at` / `redispatch_last_at` / `redispatch_count` on `tb_mf_operation`
  (forward) + `tb_mf_workflow_checkpoint` (reverse), keyed by `(workflow_id, seq)`; epoch anchored once.
- **Split `DispatchResult::Pending`** → `PendingObserved` (GET/resubmit 202 — confirmed; the **only** thing
  that advances the timer) vs `PendingUncertain` (transport/5xx/unreachable — plain `_defer_pending`, no timer).
- **SP is the atomic authority** (mirrors `sp_mf_workflow_reconcile_defer`): on **`defer`** it advances the
  timer **and** clears the lease **and** sets `next_attempt_at` **and** appends the event in ONE fenced txn
  (no separate `_defer_pending`); on **`redispatch`** it advances the timer and **keeps** the lease (runner is
  about to PUT). Explicit guardrails (forward: op exists / requested / id-match; reverse: checkpoint exists /
  active / top / reverse-id-match / already-blocked idempotence). Event-time skew is **not** a block here:
  a non-increasing `event_ts` folds to `appended=0` (no audit event) but the defer/redispatch still
  transitions (mirrors the #2 within-budget-defer skew handling, not its block-path rejection).
- **No exhaustion/block** — re-PUT is idempotent and escalates indefinitely; a broken op fails *definitively*
  via the rerun's 400 → reversal. (A future #2-style bound is possible but out of scope.)
- **Scope:** all recovered-pending arms — **planned forward (~runner.drift:1950)**, **legacy single-op
  forward (~888)**, **reverse compensation** — via one shared `_pending_redispatch_or_defer` helper.
- **Config:** `deployment.pending_redispatch_after_ms` (int ≥ 0, default 60000 ≈ participant lease TTL +
  margin; strict startup validation; test-override short).

## Review rounds folded in

K review round 1 (5 findings): split Pending (confirmed vs uncertain) · SP-atomic defer (lease release in the
same txn) · explicit SP guardrails (forward + reverse, mirroring #2) · include the legacy single-op pending
arm (not planned-only) · added this PROGRESS.md (work-folder convention).

## Build plan — ALL STEPS DONE (the original gated plan, for the record)

1. ✅ Migration `0003_pending_redispatch.sql` (forward + reverse columns; + schema files for fresh installs).
2. ✅ `sp_mf_operation_pending_defer` + `sp_mf_checkpoint_pending_defer` (fenced, atomic defer, guardrails) +
   host wrappers. SP-regression tests (148/148).
3. ✅ `DispatchResult` split (`PendingObserved`/`PendingUncertain`) + the `_reconcile`/`_classify_dispatch`
   remap.
4. ✅ `_pending_redispatch_or_defer` (+ `_checkpoint_…`) helper wired at all three recovered-pending arms.
5. ✅ Config + strict validation; contract docs (`microflows_design.md` §5.1.1).
6. ✅ Tests: uflowsd integration pin `c12_*` (208/208); reverse-compensation SP coverage. **Bookkeeper case
   [12] xfail→pass is the app team's repo** (out of repo here — the stub + `c12_*` are the in-repo equivalents).

## Files (built)
- `microflows/db/migrations/0003_pending_redispatch.sql` + the two schema files (`tb_mf_workflow_operation.sql`,
  `tb_mf_workflow_checkpoint.sql`).
- `microflows/db/procs/sp_mf_operation_pending_defer.sql`, `sp_mf_checkpoint_pending_defer.sql`
- `microflows/db-tests/sp_operation_test.py` (forward + reverse pending-defer coverage)
- `microflows/packages/microflows/src/host.drift` (wrappers + decoded outcome variants + export list)
- `microflows/runner/src/runner.drift` (DispatchResult split + shared helpers at 3 arms + config + validation)
- `microflows/doc/microflows_design.md` (§5.1.1 PUT-owns-reclaim / GET-read-only)
- `integration/coordinator-singular/test.py` (+ coordinator-fixtures CSVs extended for the new columns)
- docs + integration tests
