# reversal-compensation — Progress / status

Status snapshot for restart context. Design + the full per-round change log live
in [README.md](./README.md); this file is the at-a-glance "where it stands +
literal next action".

## Status: **sub-steps A–D COMPLETE** (full reversal/compensation slice, proven E2E)

The whole slice works end-to-end: a manual-IR forward PLAN runs ≥2 operations,
BUILDING a checkpoint stack; a definite forward failure of a later op automatically
BEGINS reversal and unwinds the stack highest→lowest through the generic dispatcher,
crash-safely and idempotently, reaching `reversed(5)` — or `blocked_resolution(3)`
only when automatic reversal can't continue. Forward and reverse both resume
correctly from durable operation/checkpoint state after restart, and survive lost
acks.

## Sub-step ledger
- [x] **A — reversal transitions + reverse loop against ONE active checkpoint.**
  Proven E2E (normal unwind, terminal idempotency, lost-ack reconcile, restart
  recovery, no-active-checkpoint defer, definite-rejection block, no-binding
  defer).
- [x] **B — stack traversal** with multiple seeded checkpoints. Proven E2E
  through the runner loop: full unwind highest→lowest (exec +2, order from the
  audit events, distinct per-seq ids + own inputs), terminal idempotency
  (no double compensation), mid-stack restart (head advances, resume compensates
  only the remaining checkpoint), and lost-ack on the lower checkpoint
  (effectively-once across the stack).
- [x] **C — multi-operation forward PLAN (manual IR)** — the forward runner now
  BUILDS the stack. `operation_settle` generalized with `arg_is_final`
  (intermediate = checkpoint + stay forward + retain lease; final = complete);
  `_run_planned`/`_run_forward` execute an ordered config `plan` (own seq + stable
  id per step, per-seq recovery, no loops/branches), reusing the generic dispatcher
  + transition procs. Proven: fresh 2-op plan builds the stack + completes; mid-plan
  restart resumes from durable state (skips settled op).
- [x] **D — forward failure → automatic reversal** — a definite rejection of a later
  op calls `begin_reversal` + unwinds in the same drive (`_begin_reversal_unwind`).
  Proven: op1 success → op2 reject → reversal → compensate op1 → `reversed`, with
  durable evidence, restart across the transition, and lost-ack on both the forward
  op and the compensation.

## What's done (layer by layer)
- **Proc layer** — `sp_mf_workflow_begin_reversal`,
  `sp_mf_checkpoint_reverse_request` / `_settle` / `_block` / `_head`, and the
  `sp_mf_operation_dispatch_defer` generalization (fence forward(1) OR
  reversing(2)). Fenced, time-disciplined, idempotent, reverse-order enforced,
  replay bound to durable identity + the full compensation binding.
- **Host methods** — typed domain-outcome variants for all five reversal procs;
  positive decodes exercised against live MariaDB in `live_reversal_test`.
- **Runner reverse loop** — `_run_reversal` reads the authoritative
  `reverse_head`, looks up the manual-IR compensation binding, persists via
  `reverse_request`, then dispatches/recovers via `_compensate(recover)`:
  a FRESH (pending) checkpoint dispatches PUT-first (`_classify_dispatch`); a
  durably-DISPATCHED checkpoint recovers **GET-first** (`_reconcile`), never a
  blind re-PUT. Then settles / defers / blocks.
- **Runner forward plan loop** — `_run_planned` (create/claim/reversing-check) +
  `_run_forward` execute an ordered config `plan`: per-step `operation_seq` + stable
  `_operation_id`, checkpoint payload = op input, per-seq recovery
  (`operation_result` skip / durable request resume / fresh), intermediate vs final
  settle. A later-op definite rejection → `_begin_reversal_unwind` (begin_reversal +
  `_run_reversal` in the same drive); first-op rejection → block.
- **`operation_settle` proc/host** — `arg_is_final` distinguishes intermediate
  (checkpoint + stay forward(1) + retain lease + `operation_settled` event) from
  final (complete + `workflow_completed`). For a pinned plan it rejects out-of-plan
  settles (`plan_violation`: seq out of `[1, plan_length]`, `checkpoint_seq ≠ seq`, or
  finality ≠ `seq == plan_length`) — finality DERIVED, not caller-trusted.
- **Durable plan pin** — `tb_mf_workflow_plan` (plan_hash + plan_length) pinned
  ATOMICALLY by `sp_mf_workflow_create_planned` (creation + pin = one command;
  `plan_conflict` on a changed plan OR a changed immutable SCRIPT identity; the plan
  row is created only here, never orphaned). The runner's `_plan_hash(cfg, plan)`
  covers the FULL contract — name + input + resolved op `schema_version` + LOGICAL
  participant id + compensation (op + version + participant) — and pins before ALL
  state branches. A config change to an in-flight workflow's plan (input, contract,
  OR participant) defers; it never runs a different plan.
- **Durable request ordering** — `operation_request` is plan-aware: it rejects
  (`plan_violation`) a seq outside `[1, plan_length]` or (seq > 1) whose predecessor
  hasn't settled, BEFORE the remote side effect.
- **Stub** — `reserve` forward op (compensable via `reserve→release`), `release`
  compensation op, a `_fault.reject` definite-400 injector, and a PUT-only
  `put_count` (+`/debug/put-count`) so GET-first reconcile is observable.

## Verification (current — all green)
Run from repo root with `MDB_ROOT_PWD`, `DRIFT_TOOLCHAIN_ROOT`, `DRIFT_PKG_ROOT`
set:
```
just test
```
All green (authoritative pass counts are each runner's own `N/N` line):
- singular unit/e2e; microflows unit/e2e + `sp_operation` regression (incl. the
  intermediate-settle coverage); integration/coordinator-singular (forward +
  single-checkpoint reversal + multi-checkpoint stack + forward-plan + forward-fail
  reversal).

Reversal-side integration assertions: `reverse_to_reversed`,
`reverse_terminal_idempotent`, `reverse_lost_ack` (exec delta == 1, req delta
≥ 2), `reverse_restart_recovery` (consistent + transition-faithful post-request
seed; **GET-first** — exec delta == 0, put delta == 0, req delta == 1),
`reverse_no_active_checkpoint`, `reverse_block_on_rejection` (classified reason in
the runner response), `reverse_block_durable_state` (DB read-back: workflow
`blocked_resolution(3)`/reverse, checkpoint `resolution_required(3)`, a
`compensation_blocked` event with the reason), `reverse_block_no_redispatch`,
`reverse_no_compensation_binding`.

Multi-checkpoint stack assertions: `reverse_stack_unwind` (seq2→seq1,
exec +2, order from audit events, distinct ids, own inputs `b1`/`b2`),
`reverse_stack_idempotent` (terminal re-run, no double compensation),
`reverse_stack_restart_midstack` (seq2 pre-reversed, resume compensates only seq1,
exec +1), `reverse_stack_lost_ack` (lower checkpoint drops ack → reconcile →
reversed, effectively-once across the stack, exec +2).

Forward-plan + forward-fail assertions: `forward_plan_builds_stack` (fresh 2-op
plan → completed, 2-checkpoint stack, own inputs `c1`/`c2`, distinct ids, exec +2),
`forward_plan_resume` (seeded mid-plan `a0..20`, skip op1, run op2, exec +1),
`forward_fail_begins_reversal` (op2 reject → reversal → `reversed`, exec +2),
`forward_fail_reverses_durable` (DB: reversed(5)/reverse, checkpoint reversed(2),
`reversal_begun`→`compensation_settled`), `forward_fail_restart` (seeded `a0..21`,
restart re-dispatches op2 → reversal → `reversed`), `forward_fail_lost_ack`
(forward op + compensation both reconcile → `reversed`, exec +2, GETs occurred),
`forward_plan_conflict` (a changed plan against a pinned workflow → defer, no op runs).

## Sub-step C/D review round 4 (this round — schema FK + replay coverage)
- **Plan FK (Medium)** — `tb_mf_workflow_plan` gains a FOREIGN KEY to `tb_mf_workflow`
  (matching operations/checkpoints/events); orphan-prevention is now structural.
- **Multi-op terminal replay (Medium)** — `terminal_rerun_multiop_final_result`:
  completed PLAN workflow re-run with the participant down returns the FINAL op result.
- **Doc (Low)** — `_run_forward` header corrected (first-op rejection reverses).

## Sub-step C/D review round 3 (prior round — ordering, routing, model fidelity)
- **Durable request ordering (High)** — `operation_request` rejects out-of-range or
  predecessor-incomplete requests before dispatch (`plan_violation`).
- **Participant pinned in hash (High)** — `_plan_hash` includes the logical participant
  id (forward + comp); re-routing → `plan_conflict` (`forward_plan_participant_conflict`).
- **First-op rejection reverses (High)** — plan path always begins reversal; no
  checkpoints → `reversed` (`forward_first_reject_reverses`). Legacy `--operation` path
  unchanged (pre-IR).
- **Script identity in replay (Medium)** — `create_planned` conflicts on a changed
  script_name/revision even with the same plan hash.
- **Compensation only for non-final steps (Medium)** — `_validate_plan` requires it for
  all but the last; single-op / non-compensable-final plans are valid
  (`forward_plan_single_noncompensable`).

## Sub-step C/D review round 2 (prior round — pin completeness + atomicity)
- **Plan hash = full contract (High)** — `_plan_hash(cfg, plan)` now hashes resolved
  op `schema_version` + compensation (op + version) per step, so a registry contract
  change conflicts too (`forward_plan_contract_conflict`).
- **Atomic create+pin, no bypass (High)** — `sp_mf_plan_pin` → `sp_mf_workflow_create_planned`
  (creation + pin one command), validated before fresh/resume/terminal/reversing
  branches. Pin tied to creation (no orphan, creator-decided). Removed plan_pin proc + host.
- **Settle rejects out-of-plan (Medium)** — `plan_violation` (seq range / checkpoint /
  finality), checks before the op load (so seq-out-of-plan ≠ operation_not_found).
- **Input type at startup (Medium)** — `_build_plan` requires each step input be a JSON
  object before any claim.
- Recomputed seed plan-hash pins; SP `create_planned` + `plan_violation` coverage.

## Sub-step C/D review round 1 (prior round — durable IR hardening)
- **Plan pinning (High)** — `tb_mf_workflow_plan` + `sp_mf_plan_pin` + runner
  `_plan_hash`/pin → a config change can't alter an in-flight workflow's plan
  (`plan_conflict` → defer). Validated: my Python plan-hash matches the runner's
  (C2/D2 resume + C3 conflict all green).
- **Finality derived (High)** — `operation_settle` enforces
  `is_final == (seq == plan_length)` from the pin (before replay) → no early completion.
- **Terminal returns final op (High)** — `_report_terminal`/`_inspect_report` take the
  result seq; plan path passes `plan.len`.
- **Compensation required per step (High)** — `_validate_plan` rejects a non-compensable
  plan step.
- **Recovered forward GET-first (Medium)** — `_run_forward` uses
  `_dispatch_or_reconcile(recovered)` (renamed from `_dispatch_compensation`, now shared).
- **Faithful seeds + lost-ack invariant (Medium)** — mid-plan seeds gain
  `operation_requested` events + real hashes + plan pins; `forward_fail_lost_ack`
  asserts the reconcile invariant (delayed forward reconcile races commit visibility →
  exact wire shape varies; effectively-once + GETs-occurred is the deterministic proof).

## Sub-steps C + D (prior round — forward plan + forward-failure reversal)
- **`operation_settle` generalized** (`arg_is_final`): intermediate settle
  checkpoints + stays forward + retains lease; final completes. Host + SP updated
  (intermediate-settle SP coverage added).
- **Runner forward plan** (`_run_planned`/`_run_forward`): config `plan` array
  (ordered, no loops/branches), per-seq durable recovery, checkpoint payload = op
  input. A later-op definite rejection → `_begin_reversal_unwind` (begin_reversal +
  unwind in one drive). Stub gained the `reserve` forward op.
- Seeded forward mid-plan fixtures `a0..20` (resume) and `a0..21` (restart across the
  forward→reversal transition). All sub-step C + D requirements proven E2E.

## Sub-step B (prior round — multi-checkpoint stack traversal)
- Runner already supported full-stack unwind (`_run_reversal` re-reads
  `reverse_head` until terminal); B added the integration proof + mid-stack
  recovery fixtures `a0..0b–0d` (two active checkpoints each). No Drift source
  changed — fixtures + `test.py` only.
- Covers every sub-step-B requirement: highest→lowest order, own
  binding/input/invocation-id per checkpoint, intermediate-settle stays reversing
  + head advances, restart + lost-ack recovery between checkpoints, final settle →
  `reversed`, no checkpoint compensated twice.

## Review round 5 (prior round — addressed)
- **Medium** — WF7 checkpoint is fully transition-faithful: `reverse_input_hash`
  is the exact runner-derived value (`d932b54d…984f`, validated against WF5's
  runner-persisted hash for the same algorithm), not the placeholder `rh7`;
  `updated_at` advanced to the request event ts (`00:00:01`).
- **Low** — `reverse_restart_recovery` also asserts request-delta == 1 (a GET
  occurred), proving GET-first reconcile rather than zero participant interaction.
- **Low** — reconciled the milestone count (no stale `24/24` alongside `25/25`).

## Review round 4 (prior round — addressed)
- **Medium** — restart recovery is now genuinely GET-first: `_compensate` takes a
  `recover` flag; the Dispatched branch reconciles via `_reconcile` (GET-first),
  not `_classify_dispatch` (PUT-first). Proven by the new `put_count` (put-delta
  0), not merely participant idempotency.
- **Medium** — WF7 (`a0..07`) is now transition-faithful: the
  `compensation_requested` event carries the full proc payload (id + reverse op +
  schema version) and a strictly-increasing timestamp; workflow `current_event_ts`
  matches.
- **Medium** — the rejection case verifies durable evidence, not just the runner
  response: a new read-only `_mdb()` helper asserts checkpoint
  `resolution_required`, the `compensation_blocked` event reason, and the
  blocked/reverse workflow state.
- **Low** — removed the stale duplicated next-step text after "Sub-step A
  complete" in `README.md`; integration README + counts updated to 25.

## Uncommitted worktree (sub-steps B + C + D + C/D review rounds 1-3)
Sub-step A + its review rounds landed in `2e57e4a`. Not yet committed:
- **Schema/procs:** `db/schema/tb_mf_workflow_plan.sql` (new) + `db/procs/sp_mf_plan_pin.sql`
  (new); `db/procs/sp_mf_operation_settle.sql` (`arg_is_final` + plan-derived finality).
- **Host:** `packages/microflows/src/host.drift` — `operation_settle` `is_final` +
  `FinalityMismatch`, new `plan_pin` + `PlanPinOutcome` (added to the `export {}` list).
- **Runner:** `runner/src/runner.drift` — plan loop (`_run_planned`/`_run_forward`),
  `_plan_hash`/pin, `_begin_reversal_unwind`, recovered-GET-first
  (`_dispatch_or_reconcile`), terminal-final-op, compensation-required validation.
- **Stub:** `participant-stub/src/app.drift` — `reserve` forward op.
- **Fixtures:** `coordinator-fixtures/*.csv` — sub-step B stacks `a0..0b–0d`; C/D forward
  mid-plan seeds `a0..20`/`a0..21` (transition-faithful) + plan-conflict seed `a0..22`;
  new `tb_mf_workflow_plan.data.csv` (pins).
- **Tests:** `db/tests/sp_operation_test.py` (intermediate-settle + plan_pin + finality),
  `integration/coordinator-singular/test.py` + `README.md` (B stack + C/D + plan-conflict).
- **Docs:** `work/reversal-compensation/README.md` + `Progress.md`.

Last landed commits: `f3ef6b5` (transition layer), `88df06c` (checkpoint
primitives), `e6983d2` (crash-safe compensation execution), `2e57e4a` (execute
durable workflow compensation — sub-step A E2E + review rounds).

## Next action
The reversal/compensation slice (A–D) is COMPLETE and green. Next on the roadmap
(§7): the portable **ScriptRegistry**, then the **parser** — replacing the config
`plan`/`operations` manual IR with compiled script output. The forward plan loop and
the compensation binding lookup are the seams those will plug into. (Out of scope
still: authorized administration OUT of `blocked_resolution` — a follow-up
milestone.)

## Boundaries (unchanged)
- Reversal owns only ENTRY into `blocked_resolution`; authorized administration
  OUT of blocked (retry / resolve / accept-exception) is a follow-up milestone.
- Reversal begins only on a DEFINITE forward rejection after a durable request
  exists; an UNCERTAIN forward outcome stays `blocked(forward)` — never
  compensate from an uncertain forward outcome.
