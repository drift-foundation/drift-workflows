# uflowsd pending→re-dispatch recovery (Phase 7 case [12]) — implementation spec

## Problem (recap)

A participant that **commits its side effect and crashes before `Singular.complete`** leaves its idempotency
record `working` (lease yet to expire). uflowsd's recovery path (`_reconcile`, GET-first) sees that op as
**`Pending`** and only ever re-dispatches a PUT on `Unknown(404)` — never on `Pending`. So a crashed-mid-op
participant shows as `Pending` forever, uflowsd never re-PUTs, the participant's reclaim-on-PUT path is never
triggered, and the workflow stays **pending** (ledger correct, exactly-once). Decision: **Option A — recovery
is via byte-identical PUT; GET stays a pure read.** Escalation state must be **durable + fenced** (not an
in-memory timer), mirroring the #2 reconcile-budget machinery.

## DispatchResult: split Pending (confirmed vs uncertain)

`DispatchResult::Pending` today conflates two cases — `_reconcile` maps **GET 202** (`GetOutcome::Pending`,
a *confirmed* participant pending) **and** transport failure / 5xx / unreachable (`GetOutcome::TransportFailed`)
to the same `Pending`. The redispatch timer must advance **only on a confirmed participant pending**, never on
network uncertainty. So split it:
- **`DispatchResult::PendingObserved`** — a GET 202 (or a resubmit-202): the participant *confirmed* the op is
  in progress. **Only this feeds `pending_defer`** (the durable timer) on a **recovered** dispatch.
- **`DispatchResult::PendingUncertain`** — transport failed / 5xx / unreachable: outcome unknown. Keep the
  **existing `_defer_pending`** behavior (release + poll), **no timer advance** (an unreachable participant
  must not be escalated toward a reclaim it may not need).

Update the `_reconcile` / `_classify_dispatch` GET/PUT mappings accordingly (202 → PendingObserved; transport
→ PendingUncertain). A FRESH (non-recovered) dispatch ignores the timer entirely.

## Flow (forward + reverse; both go through recovered dispatch)

1. Recovered op/checkpoint still starts **GET-first** (`_reconcile`).
2. **GET → `PendingObserved`** (a confirmed 202; `PendingUncertain` → plain `_defer_pending`, no timer) ⇒
   advance a **durable, fenced** pending-redispatch timer (SP below):
   - first observation anchors `redispatch_first_seen_at` (anchored ONCE; never reset by retry/resume);
   - `elapsed = db_now − COALESCE(redispatch_last_at, redispatch_first_seen_at)`;
   - `elapsed < pending_redispatch_after_ms` ⇒ outcome **`defer`**: the SP releases the lease + sets
     `next_attempt_at` (atomically); the runner just returns pending (it does **not** call `_defer_pending`);
   - `elapsed ≥ pending_redispatch_after_ms` ⇒ outcome **`redispatch`** + re-arm (`redispatch_last_at = db_now`,
     `redispatch_count += 1`).
3. **`redispatch`** ⇒ issue a **byte-identical PUT** for the same `operation_id` + `input` (the reverse path
   re-PUTs the pinned compensation `reverse_invocation_id` + `reverse_input`). Classify the response:
   - **202** (live-working participant; PR2 `resume → Active`, lease not stolen) ⇒ back to step 2 (timer
     re-armed; escalates again after another interval);
   - **200** (expired-working ⇒ participant reclaimed via Singular `resume → Granted`, reran idempotently,
     completed) ⇒ settle as usual;
   - 400/409/404/transport ⇒ existing classify paths (definite-reject → reversal; route-404 → the **#2**
     budget; transport → defer).
4. Terminal settle ⇒ the redispatch state is **ignored** (only read while the op is `requested`/pending; left
   as audit, no explicit clear — same as `reconcile_*`).

**No exhaustion/block** (unlike #2): a re-PUT is idempotent and safe, so pending→re-dispatch escalates
indefinitely. A genuinely broken op fails *definitively* (the rerun returns 400 → reversal), not infinite
pending; a slow-but-alive op keeps answering 202. (A future bound could be added with the #2 column shape if
operationally needed — out of scope here.)

## Durable state (mirror `reconcile_*`)

Migration `000N_pending_redispatch.sql`, columns added to BOTH tables (parallel to the #2 budget columns):
- `tb_mf_operation` (forward) and `tb_mf_workflow_checkpoint` (reverse):
  - `redispatch_first_seen_at datetime(6) NULL`  — anchored once; the escalation epoch.
  - `redispatch_last_at datetime(6) NULL`        — last escalation (re-arm anchor).
  - `redispatch_count int NOT NULL DEFAULT 0`    — escalations issued (audit/observability).
- Keyed by `(workflow_id, operation_seq)` / `(workflow_id, seq)`, so resume re-reads the same row and the
  epoch never resets (same discipline as `reconcile_first_seen_at`).

## SPs (fenced; mirror `sp_mf_workflow_reconcile_defer` exactly — incl. its atomic defer)

`sp_mf_operation_pending_defer` (forward) / `sp_mf_checkpoint_pending_defer` (reverse):
- IN: `workflow_id, executor, fencing_token, operation_seq|seq, operation_id|invocation_id, db_now,
  next_attempt_at, event_ts, pending_redispatch_after_ms`.
- **Fence + guardrails BEFORE any mutation** (mirror #2 — a SELECT … FOR UPDATE then validate):
  - **Forward** (`sp_mf_operation_pending_defer`): the workflow row is **leased by this executor + token +
    `state=forward(1)`** (else `fence_lost`); the **op row exists**, `status=requested(1)`, and
    `operation_id` **matches** the supplied id (else a structured non-mutating outcome —
    `operation_not_requested` / `operation_conflict`, exactly like the #2 forward SP).
  - **Reverse** (`sp_mf_checkpoint_pending_defer`): mirror the #2 **reverse** guardrails — workflow leased +
    `state=reversing(2)`; the **checkpoint exists**, is `requested/active` and the **TOP** active checkpoint,
    the **reverse invocation id matches** the pinned `reverse_invocation_id`; **already-blocked is an
    idempotent no-op** (`resolution_required` → return without mutating); and **event-time skew**
    (`arg_event_ts <= current_event_ts`) is rejected **before** any write.
- Anchor `redispatch_first_seen_at` if NULL; `elapsed = (db_now − COALESCE(redispatch_last_at,
  redispatch_first_seen_at))`. Time discipline: caller-supplied `db_now`/`event_ts`, never `NOW()`.
- **`elapsed ≥ pending_redispatch_after_ms` → outcome `redispatch`:** advance the timer (`redispatch_last_at =
  db_now`, `redispatch_count += 1`) + append a `pending_redispatch` event, and **KEEP the lease** (the runner
  is about to PUT under the held lease). No settle/checkpoint.
- **else → outcome `defer`:** in **ONE fenced transaction** — advance/anchor the timer **AND clear the lease**
  (`lease_owner = NULL`) **AND set `next_attempt_at = arg_next_attempt_at`** **AND** append the
  `pending_deferred` event (exactly the #2 deferred branch, lines 184–187). The runner must NOT separately
  `_defer_pending` after a `defer` outcome — the release already happened atomically, so a crash between the
  timer update and the release is impossible.
- Result doc: `{"outcome":"redispatch"|"defer"|"fence_lost"|"operation_not_requested"|…, "redispatch_count":N}`.
- Host wrappers `operation_pending_defer` / `checkpoint_pending_defer` + decoded outcome variants
  (`host.drift`).

## Runner changes (`runner.drift`)

Factor the escalation into ONE shared helper so every recovered-pending arm behaves identically:
`_pending_redispatch_or_defer(host, workflow_id, fencing_token, seq/op_id, recovered, base/op/input,
admission)`:
- if **not `recovered`** → `_defer_pending` (fresh dispatch never escalates; unchanged).
- if `recovered` → call the pending-defer SP:
  - **`defer`** → return `Outcome::Pending` directly (the SP **already** released the lease + set
    `next_attempt_at`; do **not** call `_defer_pending` again).
  - **`redispatch`** → byte-identical `_dispatch_put` (lease still held) → classify: **200** → the existing
    Done/finality-probe/settle path; **202 / PendingObserved** → `_defer_pending` (release + poll; the timer
    is re-armed, so it escalates again next interval); **PendingUncertain / transport** → `_defer_pending`,
    no further timer change; **400/409/404** → existing reject/route-404 paths.
  - **`fence_lost` / guardrail outcome** → the existing fence-lost/abort handling.

Apply this helper at **all** recovered-pending sites — only on **`PendingObserved`** (a confirmed 202),
never on `PendingUncertain`:
- **Planned forward** (`~runner.drift:1950`, `DispatchResult::PendingObserved`).
- **Legacy single-op forward** (`~runner.drift:888`, the `--config` single-operation path — same
  `DispatchResult::PendingObserved` arm). *(Included, not scoped out: the crash-recovery window applies to
  both paths; one shared helper keeps them in lock-step.)*
- **Reverse compensation** (the reverse-dispatch reconcile arm), via `checkpoint_pending_defer` + a
  byte-identical reverse re-PUT of the pinned `reverse_*` binding.

`PendingUncertain` everywhere keeps the plain `_defer_pending`. The escalation PUT reuses `_dispatch_put` (no
new transport path).

## Config

- `deployment.workflow_call`-sibling: **`deployment.pending_redispatch_after_ms`** — integer **≥ 0**;
  **default `60000`** (≈ a typical participant Singular lease TTL + margin; SHOULD be set ≥ the deployment's
  participant lease TTL + margin). Validated strictly at startup (no silent fallback), like the #2
  `reconcile_budget`. **Test override:** a small value (e.g. `100` or `0` = escalate on the first recovered
  poll) so integration tests don't wait a real TTL.

## Contract docs (`microflows_design.md` participant contract)

- **PUT** `{operation_id}` + same input is an **idempotent reassert/reclaim**, not a fresh execution.
- A **live-working** op ⇒ PUT returns **202** (no reclaim; never steal a live lease — PR2 §4).
- An **expired-working** op ⇒ PUT MUST **reclaim** (Singular `resume`), **rerun idempotently**, and
  **complete / replay** the recorded result (→ 200).
- **GET** is **read-only** and **never owns reclaim** (returns terminal | pending | unknown only).

## Tests

- **Bookkeeper Phase 7 `harness/run_ledger_stress.py` case [12]** (strict xfail) → **pass**: the escalation
  PUT appears (T6 reclaim log) and the workflow completes; ledger applied once.
- **uflowsd integration pin** (new `composition`/recovery case, root gate): first PUT commits then participant
  "crashes" before complete; recovered GET → 202; after `pending_redispatch_after_ms` (test-short) uflowsd
  re-PUTs same id/input; participant reclaims; workflow **completes**; ledger applied **once**. Assert the
  durable `redispatch_count` advanced and the op settled exactly once.
- **Reverse-compensation equivalent** (if feasible): a compensation whose participant crashes mid-reverse →
  checkpoint pending-redispatch escalates → reclaim → compensation completes.
- SP regression: `pending_defer` fence_lost on a stale token; the **guardrails** (op/checkpoint not
  requested/active, id mismatch, reverse not-top, already-blocked idempotence, event-time skew → no mutation);
  epoch anchored once across resume; defer↔redispatch threshold at the boundary; **`defer` is one atomic txn**
  (lease cleared + `next_attempt_at` set + timer advanced + event appended together — assert no partial state).
- Runner: **`PendingUncertain` (transport/5xx) does NOT advance the timer** (only a confirmed 202 does); a
  **fresh** (non-recovered) dispatch never escalates.

## Invariants
- Re-PUT is **idempotent** (operation_id/input_hash + the participant's Singular dedup); safe to issue while
  live (→202).
- Escalation is **durable + fenced** (survives crash/resume; a lost lease can't escalate).
- **GET stays pure.** Recovery is coordinator-driven via PUT — no participant GET-reclaim, no bookkeeper
  workaround.
