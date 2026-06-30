# uflowsd pending→re-dispatch recovery — PROGRESS

## Status

**Spec phase.** `SPEC.md` holds the implementation-ready design for the Phase 7 case-[12] recovery gap:
uflowsd, on a *recovered* operation whose participant crashed mid-op, must re-dispatch a **byte-identical
PUT** (not GET-poll forever) so the participant reclaims its expired Singular lease and completes. **Option A
confirmed** (re-dispatch via PUT; GET stays a pure read). No code yet.

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
  active / top / reverse-id-match / already-blocked idempotence / event-time skew before mutation).
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

## Build plan (each gated; not started)

1. Migration `000N_pending_redispatch.sql` (forward + reverse columns).
2. `sp_mf_operation_pending_defer` + `sp_mf_checkpoint_pending_defer` (fenced, atomic defer, guardrails) +
   host wrappers. SP-regression tests.
3. `DispatchResult` split (`PendingObserved`/`PendingUncertain`) + the `_reconcile`/`_classify_dispatch`
   remap.
4. `_pending_redispatch_or_defer` helper wired at all three recovered-pending arms.
5. Config + strict validation; contract docs (`microflows_design.md` participant contract).
6. Tests: bookkeeper case [12] xfail→pass; uflowsd integration pin; reverse-compensation equivalent; root
   `just test` green.

## Open / to confirm

- `pending_redispatch_after_ms` default (60000) vs the app team's real participant Singular lease TTL — confirm
  it's ≥ TTL + margin for production.
- Reverse-compensation integration coverage feasibility (the harness may not crash mid-reverse easily).

## Files (when built)
- `microflows/db/migrations/000N_pending_redispatch.sql`
- `microflows/db/procs/sp_mf_operation_pending_defer.sql`, `sp_mf_checkpoint_pending_defer.sql`
- `microflows/packages/microflows/src/host.drift` (wrappers)
- `microflows/runner/src/runner.drift` (DispatchResult split + shared helper at 3 arms + config)
- docs + integration tests
