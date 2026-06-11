# reversal-compensation — Progress / status

Status snapshot for restart context. Design + the full per-round change log live
in [README.md](./README.md); this file is the at-a-glance "where it stands +
literal next action".

## Status: **sub-step A COMPLETE** (proc + host + runner loop, proven E2E)

Durable compensation dispatch works end-to-end against a single active
checkpoint: a `reversing(2)` workflow unwinds by dispatching its bound REVERSE
operation through the generic dispatcher, crash-safely and idempotently,
reaching `reversed(5)` — or `blocked_resolution(3, reverse)` when automatic
reversal can't continue.

## Sub-step ledger
- [x] **A — reversal transitions + reverse loop against ONE active checkpoint.**
  Proven E2E (normal unwind, terminal idempotency, lost-ack reconcile, restart
  recovery, no-active-checkpoint defer, definite-rejection block, no-binding
  defer).
- [ ] **B — stack traversal** with multiple seeded checkpoints (reverse
  highest→lowest; one fails → blocked). *(proc layer already proves descent in
  SP tests; not yet proven through the runner loop end-to-end.)*
- [ ] **C — minimal multi-operation manual IR** — forward path runs ≥2 ops so a
  checkpoint STACK exists to reverse (today the forward runner is single-op).
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
- integration/coordinator-singular: **25/25** (16 forward + 9 reversal)

The 9 reversal integration assertions: `reverse_to_reversed`,
`reverse_terminal_idempotent`, `reverse_lost_ack` (exec delta == 1, req delta
≥ 2), `reverse_restart_recovery` (consistent + transition-faithful post-request
seed; **GET-first** — exec delta == 0, put delta == 0, req delta == 1),
`reverse_no_active_checkpoint`, `reverse_block_on_rejection` (classified reason in
the runner response), `reverse_block_durable_state` (DB read-back: workflow
`blocked_resolution(3)`/reverse, checkpoint `resolution_required(3)`, a
`compensation_blocked` event with the reason), `reverse_block_no_redispatch`,
`reverse_no_compensation_binding`.

## Review round 5 (this round — addressed)
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

## Uncommitted worktree (sub-step A E2E + review rounds)
Not yet committed:
- `microflows/runner/src/runner.drift` — `_compensate(recover)` GET-first recovery.
- `microflows/participant-stub/src/app.drift` — `release` op, `_fault.reject`,
  `put_count` + `/debug/put-count`.
- `microflows/db/scenarios/coordinator-fixtures/tb_mf_workflow_checkpoint.data.csv`
  (new) + `tb_mf_workflow.data.csv` + `tb_mf_workflow_event.data.csv` — reversing
  fixtures `a0..05–0a` (WF7 transition-faithful).
- `integration/coordinator-singular/test.py` + `README.md` — 9 reversal
  assertions (total 25), GET-first + durable-state proofs, README coverage.
- `work/reversal-compensation/README.md` + `Progress.md` — progress log + status.

Last landed commits (proc/host/runner layers): `f3ef6b5` (transition layer),
`88df06c` (checkpoint primitives), `e6983d2` (crash-safe compensation execution).

## Next action
**Begin sub-step B**: drive multi-checkpoint stack traversal through the runner
reverse loop end-to-end — seed a reversing workflow with ≥2 active checkpoints,
prove highest→lowest descent to `reversed`, and prove one mid-stack definite
failure → `blocked_resolution` with the lower checkpoints left active. (The proc
layer already enforces reverse-order descent; this proves it through the loop.)

## Boundaries (unchanged)
- Reversal owns only ENTRY into `blocked_resolution`; authorized administration
  OUT of blocked (retry / resolve / accept-exception) is a follow-up milestone.
- Reversal begins only on a DEFINITE forward rejection after a durable request
  exists; an UNCERTAIN forward outcome stays `blocked(forward)` — never
  compensate from an uncertain forward outcome.
