# reversal-compensation — Progress / status

Status snapshot for restart context. Design + the full per-round change log live
in [README.md](./README.md); this file is the at-a-glance "where it stands +
literal next action".

## Status: **sub-steps A + B COMPLETE** (single- and multi-checkpoint unwind, proven E2E)

Durable compensation dispatch works end-to-end: a `reversing(2)` workflow unwinds
its checkpoint STACK highest→lowest by dispatching each bound REVERSE operation
through the generic dispatcher, crash-safely and idempotently, reaching
`reversed(5)` — or `blocked_resolution(3, reverse)` when automatic reversal can't
continue. The runner's `_run_reversal` while-loop drives the whole stack in one
invocation and resumes correctly from any mid-stack point.

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
- [ ] **C — minimal multi-operation manual IR** — forward path runs ≥2 ops so a
  checkpoint STACK is BUILT by the forward runner (today the forward runner is
  single-op; B proved the *unwind* of a seeded stack, C builds a real one).
  Largest remaining piece.
- [ ] **D — full E2E proof** — forward op1 success → op2 definite fail →
  reverse-order compensation → `reversed`, incl. lost acks + restart.

## What's done (layer by layer)
- **Proc layer** — `sp_mf_workflow_begin_reversal`,
  `sp_mf_checkpoint_reverse_request` / `_settle` / `_block` / `_head`, and the
  `sp_mf_operation_dispatch_defer` generalization (fence forward(1) OR
  reversing(2)). Fenced, time-disciplined, idempotent, reverse-order enforced,
  replay bound to durable identity + the full compensation binding. **SP 79/79.**
- **Host methods** — typed domain-outcome variants for all five reversal procs;
  positive decodes exercised against live MariaDB in `live_reversal_test`.
- **Runner reverse loop** — `_run_reversal` reads the authoritative
  `reverse_head`, looks up the manual-IR compensation binding, persists via
  `reverse_request`, then dispatches/recovers via `_compensate(recover)`:
  a FRESH (pending) checkpoint dispatches PUT-first (`_classify_dispatch`); a
  durably-DISPATCHED checkpoint recovers **GET-first** (`_reconcile`), never a
  blind re-PUT. Then settles / defers / blocks.
- **Stub** — `release` compensation op, a `_fault.reject` definite-400 injector,
  and a PUT-only `put_count` (+`/debug/put-count`) so GET-first reconcile is
  observable (put-delta 0) distinct from a PUT-first re-dispatch.

## Verification (current — all green)
Run from repo root with `MDB_ROOT_PWD`, `DRIFT_TOOLCHAIN_ROOT`, `DRIFT_PKG_ROOT`
set:
```
just test
```
- singular: 16/16
- microflows: 15/15 unit/e2e + `sp_operation` regression **79/79**
- integration/coordinator-singular: **29/29** (16 forward + 9 single-checkpoint
  reversal + 4 multi-checkpoint stack)

The 9 reversal integration assertions: `reverse_to_reversed`,
`reverse_terminal_idempotent`, `reverse_lost_ack` (exec delta == 1, req delta
≥ 2), `reverse_restart_recovery` (consistent + transition-faithful post-request
seed; **GET-first** — exec delta == 0, put delta == 0, req delta == 1),
`reverse_no_active_checkpoint`, `reverse_block_on_rejection` (classified reason in
the runner response), `reverse_block_durable_state` (DB read-back: workflow
`blocked_resolution(3)`/reverse, checkpoint `resolution_required(3)`, a
`compensation_blocked` event with the reason), `reverse_block_no_redispatch`,
`reverse_no_compensation_binding`.

The 4 multi-checkpoint stack assertions: `reverse_stack_unwind` (seq2→seq1,
exec +2, order from audit events, distinct ids, own inputs `b1`/`b2`),
`reverse_stack_idempotent` (terminal re-run, no double compensation),
`reverse_stack_restart_midstack` (seq2 pre-reversed, resume compensates only seq1,
exec +1), `reverse_stack_lost_ack` (lower checkpoint drops ack → reconcile →
reversed, effectively-once across the stack, exec +2).

## Sub-step B (this round — multi-checkpoint stack traversal, integration 29/29)
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

## Uncommitted worktree (sub-step B only)
Sub-step A + all its review rounds landed in `2e57e4a`. Not yet committed (the
sub-step B stack-traversal proof — fixtures + tests + docs, no Drift source):
- `microflows/db/scenarios/coordinator-fixtures/tb_mf_workflow.data.csv`
  + `tb_mf_workflow_checkpoint.data.csv` + `tb_mf_workflow_event.data.csv` —
  multi-checkpoint stack fixtures `a0..0b–0d`.
- `integration/coordinator-singular/test.py` + `README.md` — 4 stack-traversal
  assertions (total 29).
- `work/reversal-compensation/README.md` + `Progress.md` — progress log + status.

Last landed commits: `f3ef6b5` (transition layer), `88df06c` (checkpoint
primitives), `e6983d2` (crash-safe compensation execution), `2e57e4a` (execute
durable workflow compensation — sub-step A E2E + review rounds).

## Next action
**Begin sub-step C** (multi-op forward IR — the largest remaining piece): make the
forward runner execute ≥2 operations in sequence so a checkpoint STACK is BUILT by
the real forward path (today `OPERATION_SEQ=1`, single-op). Add per-operation
compensation bindings + input derivation. This sets up sub-step D (forward op1
success → op2 definite fail → reverse-order compensation → `reversed`, the full
end-to-end proof). B already proved the *unwind* side against seeded stacks.

## Boundaries (unchanged)
- Reversal owns only ENTRY into `blocked_resolution`; authorized administration
  OUT of blocked (retry / resolve / accept-exception) is a follow-up milestone.
- Reversal begins only on a DEFINITE forward rejection after a durable request
  exists; an UNCERTAIN forward outcome stays `blocked(forward)` — never
  compensate from an uncertain forward outcome.
