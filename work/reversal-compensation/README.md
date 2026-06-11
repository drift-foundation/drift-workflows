# reversal-compensation  (milestone-1 step: reversal + blocked_resolution)

## Short-term objective
Implement durable **compensation dispatch**: a forward-path failure unwinds the
committed checkpoint stack by dispatching each checkpoint's bound REVERSE
operation (via the generic dispatcher), in reverse order, crash-safely and
idempotently — reaching `reversed`, or `blocked_resolution` only when automatic
reversal cannot safely continue. Manual IR; parser still deferred.

## What already exists (reuse, don't rebuild)
- **State machine**: `state` 1=forward 2=reversing 3=blocked_resolution
  4=completed 5=reversed 6=resolved_exception; orthogonal `execution_direction`
  (1=forward 2=reverse) with the state/direction CHECK; `current_disposition`
  (0=none 1=completed 2=failed 3=cancelled 4=indeterminate). (`state.drift` +
  `tb_mf_workflow`.)
- **Checkpoint stack** (`tb_mf_workflow_checkpoint`): 1-based `seq` (reverse from
  highest active downward), `operation_name`, `operation_id`, `payload`,
  `reversal_state` (1=active 2=reversed 3=resolution_required 4=resolved),
  `reverse_invocation_id`, `reversed_at`, `resolution_event_seq`. A forward
  success already creates an active checkpoint (`sp_mf_operation_settle`).
- **Generic dispatcher** (runner): persist-request → PUT → lost-ack GET reconcile
  → settle; classification into Done / Pending(defer) / Rejected; registry-based
  participant resolution; `sp_mf_operation_dispatch_defer` durable deferral.

## Design (the 6 scope points, made concrete)
1. **Persist reversal intent + continuation.** A forward operation's DEFINITE
   failure (participant 400/409, or an authorized resolve-as-failed) transitions
   `forward(1) → reversing(2)`, `direction → reverse`, disposition `failed`,
   continuation → a reverse-cursor `{pos:"reverse", seq:<top active>}`, and
   appends a `reversal_begun` event. If there are NO active checkpoints, go
   straight to `reversed(5)` (trivial unwind — nothing to compensate). This
   REPLACES today's `operation_fail → blocked_resolution` for definite forward
   failures (a definite forward failure now reverses, not blocks). A forward
   INDETERMINATE outcome still → `blocked(forward)` (§3.1), unchanged.
2. **Dispatch compensation in reverse order.** While reversing, take the highest
   `seq` active checkpoint and dispatch its bound REVERSE operation through the
   GENERIC dispatcher; on durable reverse-success mark the checkpoint
   `reversed(2)` and descend; when the last active checkpoint is reversed →
   workflow `reversed(5)`. The reverse op's `(name, participant, input)` is bound
   in manual IR per forward operation (input derived from the checkpoint payload);
   the reverse op is a normal registered operation resolved via the registry.
3. **Crash-safe + idempotently resumable.** Per checkpoint: persist
   `reverse_invocation_id` on the checkpoint BEFORE dispatch (the stable
   participant key); dispatch; a lost ack reconciles by GET on that id; settle
   marks `reversed(2)`. Recovery resumes from the stack: highest active
   checkpoint; if it already has a `reverse_invocation_id`, reconcile (don't
   re-dispatch blindly); else dispatch. Idempotent via the id + `reversal_state`.
4. **Retryable deferral vs definite compensation failure.** Reverse dispatch
   reuses the dispatcher's classification: Pending / transport-uncertain →
   RETRYABLE deferral (release lease, retry later; workflow stays `reversing`);
   DEFINITE rejection, or indeterminate after reconcile exhaustion → point 5.
5. **`blocked_resolution` only when automatic reversal can't continue.** A
   compensation that fails nonretryably (or is indeterminate) sets the checkpoint
   `resolution_required(3)` and the workflow `blocked_resolution(3, dir=reverse)`
   with a diagnostic event. (Authorized resolution OUT of blocked — resolve/retry/
   accept-exception → reversing / resolved_exception — is admin machinery; scope
   minimally now: enter blocked correctly + record evidence; the resolve actions
   can be a follow-up.)
6. **Prove forward failure → reversal → reversed**, including lost acks on the
   reverse dispatch and restart recovery mid-stack.

## New procs (sketch)
- `sp_mf_workflow_begin_reversal` — `forward(1)+definite-fail → reversing(2)` (or
  `reversed(5)` if no active checkpoints); fenced; event `reversal_begun`.
- `sp_mf_checkpoint_reverse_request` — set the top active checkpoint's
  `reverse_invocation_id` + advance the reverse cursor BEFORE dispatch; fenced;
  idempotent; event `compensation_requested`.
- `sp_mf_checkpoint_reverse_settle` — checkpoint `reversed(2)` + `reversed_at`;
  last active → workflow `reversed(5)`; else advance cursor; fenced; idempotent;
  event `compensation_settled`.
- `sp_mf_checkpoint_reverse_block` — checkpoint `resolution_required(3)` +
  workflow `blocked_resolution(3,reverse)`; fenced; event `compensation_blocked`.
- Reverse retryable deferral: generalize `sp_mf_operation_dispatch_defer` to fence
  on forward(1) OR reversing(2) (today it requires forward), or a reverse variant.
- `sp_mf_checkpoint_inspect` / extend `sp_mf_workflow_inspect` so the runner can
  read the stack cursor on resume.

## Host + runner + stub + IR
- Host: methods for the new procs + reverse-checkpoint reads.
- Runner: a REVERSE loop reusing `_classify_dispatch` — drive compensations down
  the stack with the same persist→dispatch→reconcile→settle discipline; map
  Pending→defer, Rejected/indeterminate→block.
- Stub: a compensation operation (e.g. a `reserve`/`release` pair, or a generic
  reverse op that records the compensation), so a reverse dispatch is observable.
- **Manual IR (prerequisite):** the forward path must run ≥2 operations so a
  checkpoint STACK exists to reverse — today the runner is single-op
  (`OPERATION_SEQ=1`, `CHECKPOINT_SEQ=1`). Add multi-operation forward execution
  + per-operation compensation bindings (forward op → reverse op + input
  derivation). This is the largest piece and the first sub-step.

## Verification criteria
- forward op1 success (checkpoint) → op2 DEFINITE fail → reversing → compensate
  checkpoint1 → `reversed(5)`; checkpoint `reversal_state=2`.
- lost-ack on the reverse dispatch → GET reconcile → still `reversed` (compensation
  effectively-once).
- restart mid-reversal (crash after reverse_request, before settle) → resume
  reconciles by `reverse_invocation_id`, completes the unwind.
- compensation DEFINITE failure → `blocked_resolution(3,reverse)` + checkpoint
  `resolution_required`, with a diagnostic event.
- SP regressions for each new proc (fence, idempotency, terminal/stack invariants);
  integration cases for the above. Full root `just test` green.

## Confirmed sub-step sequencing (separates reversal correctness from the
## forward-runner refactor)
- **A. Reversal transitions + reverse loop against ONE active checkpoint.**
  Implement `begin_reversal` + `checkpoint_reverse_request/settle/block` + the
  runner reverse loop; prove against a SEEDED single-checkpoint reversing workflow
  (no forward refactor yet). Lost-ack reconcile + restart recovery for one
  checkpoint.
- **B. Stack traversal** with multiple SEEDED checkpoints (reverse highest→lowest;
  one fails → blocked).
- **C. Minimal multi-operation manual IR** — the forward path runs ≥2 ops creating
  a checkpoint stack + per-op compensation bindings.
- **D. Full end-to-end proof**: forward op1 success → op2 definite fail → reverse-
  order compensation → `reversed`, incl. lost acks + restart.

## Confirmed boundaries
- **Reversal owns ENTRY into `blocked_resolution`** only: correct direction,
  checkpoint `resolution_required`, durable reason, lease release, audit evidence.
  Authorized administration OUT of blocked (retry / resolve / accept exception) is
  a FOLLOW-UP milestone.
- **Reversal begins only after a DURABLE operation request exists**, and only on a
  DEFINITE forward participant rejection. A forward outcome that is UNCERTAIN
  (execution may have occurred but cannot be determined) must **stay blocked in
  the FORWARD direction** — never compensate from an uncertain forward outcome.
  So: `DispatchResult::Rejected` (definite 400/409, request already durable) →
  begin reversal; transport-uncertain/indeterminate → forward defer/retry, and on
  genuine indeterminacy → `blocked(forward)` (NOT reversal).

## Settled decisions (were open questions)
- Generalize `sp_mf_operation_dispatch_defer` to fence on forward(1) OR
  reversing(2) (one durable-deferral mechanism for both directions).
- The CHECKPOINT row is the reverse-op tracker (`reverse_invocation_id` +
  `reversal_state` + the unique-invocation key) — no new `tb_mf_operation` rows.
- Compensation input = the whole checkpoint payload for this slice.

## Progress
- [x] **Proc layer (sub-step A + B at the DB level), SP 48/48.** Four fenced,
  time-disciplined, idempotent procs + a generalization:
  - `sp_mf_workflow_begin_reversal` — forward(1)→reversing(2) (cursor at the top
    active checkpoint) or forward(1)→reversed(5) when nothing to compensate; lease
    retained while reversing, cleared on terminal. Idempotent on already-reversing/
    reversed.
  - `sp_mf_checkpoint_reverse_request` — persists `reverse_invocation_id` BEFORE
    dispatch; recovery re-request returns the persisted id (reconcile, not a second
    dispatch).
  - `sp_mf_checkpoint_reverse_settle` — checkpoint reversed(2) then DESCEND to the
    next active (stay reversing, lease retained) or reach terminal reversed(5)
    (lease cleared); idempotent on lost-ack retry of an intermediate settle.
  - `sp_mf_checkpoint_reverse_block` — checkpoint resolution_required(3) + workflow
    blocked_resolution(3) RETAINING reverse direction, lease released, audit event
    (entry-into-blocked only).
  - `sp_mf_operation_dispatch_defer` generalized to fence forward(1) OR reversing(2)
    (claim_by_id already re-claims state IN (1,2), so a reverse-defer resumes).
  - SP coverage proves: begin→reversing/reversed, stack traversal highest→lowest
    (2 checkpoints descend then terminal), request idempotency, settle idempotency,
    block → blocked(reverse)+resolution_required+lease release, fence.
- [x] **Review round 1 — preconditions enforced INSIDE the locked transition
  (SP 55/55).** Per the storage-portability principle (SPs enforce semantics, don't
  trust the runner):
  - **Reverse order** enforced: request/settle/block require `arg_seq` == the
    current TOP active checkpoint, else `out_of_order` (a runner bug can't
    compensate out of stack order).
  - **Time discipline** added to all three checkpoint procs (`arg_event_ts` must
    strictly exceed `current_event_ts`, else `event_time_skew`).
  - **Durable triggering op verified**: `begin_reversal` now takes
    `(operation_seq, operation_id)` and validates inside the lock — exists, id
    matches, status still `requested` (a settled op can't drive reversal); else
    `operation_not_found` / `operation_conflict` / `operation_not_failed`.
  - **Lease-independent terminal replay**: `already_reversed` / `already_blocked`
    are checked BEFORE the fence, so a lost-ack retry after the terminal settle/
    block (which cleared the lease) resolves correctly instead of `fence_lost`.
- [x] **Review round 2 — replay/idempotency bound to durable IDENTITY (SP 61/61).**
  - **High** — `reverse_settle` verifies `reverse_id` (and not-NULL revid) BEFORE the
    `already_reversed` replay, so after settlement a wrong/any id is
    `reverse_id_mismatch`, not accepted as the same command.
  - **High** — `reverse_block` now takes `reverse_id` and requires the checkpoint
    had a persisted `reverse_invocation_id` (a compensation was actually
    dispatched) matching it — can't block an undispatched checkpoint
    (`not_requested`) or under a foreign id.
  - **Medium** — `begin_reversal` persists the trigger op id in a new
    `tb_mf_workflow.reversal_trigger_operation_id` column; replay
    (`already_reversing/reversed`) is bound to it, so a different op row yields
    `trigger_mismatch` instead of masquerading as the same begin-reversal.
  - **Low** — added `event_time_skew` coverage for `reverse_settle` + `reverse_block`
    (previously only `reverse_request`).
  - Uniform proc order now: identity → lease-independent replay → fence → reverse
    order → time discipline → atomic mutate+audit.
- [x] **Review round 3 — begin_reversal replay recognized across ALL reverse states
  (SP 63/63).** Keyed the idempotent replay on the persisted trigger
  (`reversal_trigger_operation_id IS NOT NULL`) rather than `state IN (2,5)`, so a
  retry of the committed begin command while `blocked_resolution(3)` (or
  `resolved_exception(6)`) returns `already_begun` with the current state instead of
  `fence_lost`. Unified outcome `already_begun{state}` replaces
  `already_reversing/already_reversed`.
- [x] **Host methods (compile-clean).** `begin_reversal` / `reverse_request` /
  `reverse_settle` / `reverse_block` / `reverse_head` exposed as typed
  domain-outcome variants — domain outcomes, not DB codes.
- [x] **Durable compensation BINDING + authoritative reverse cursor (SP 67/67).**
  Pre-runner correction:
  - `tb_mf_workflow_checkpoint` gains `reverse_operation_name`,
    `reverse_schema_version`, `reverse_input_json`, `reverse_input_hash` (alongside
    `reverse_invocation_id`). `reverse_request` persists the FULL compensation
    binding BEFORE dispatch, so a later registry/manual-IR change cannot resume an
    in-flight unwind through a different contract — on recovery the pinned
    (reverse_operation_name, reverse_schema_version) resolves exactly like a forward
    pinned operation.
  - New `sp_mf_checkpoint_reverse_head` (read): the AUTHORITATIVE top active
    checkpoint. The checkpoint stack decides what to compensate next; the
    continuation is only a projection. Outcomes: none_active / pending (forward
    identity to derive the binding) / dispatched (durable pinned binding to
    reconcile). The runner resumes from THIS, not the continuation.
- [x] **Binding review (SP 69/69) — pre-runner.**
  - **High** — `reverse_request` replay is now bound to the persisted binding: it
    compares the supplied (id, reverse op name, schema version, input hash) against
    the persisted ones and returns `binding_conflict` on any mismatch (immutable
    identity, like `operation_request`); `already_requested` returns the full
    persisted binding. A racing runner with a re-derived binding can no longer
    dispatch the wrong contract under the persisted id. New host `BindingConflict`;
    `AlreadyRequested` carries the binding.
  - **Medium** — `ck_mf_checkpoint_reverse_binding` CHECK enforces the binding as
    ALL-OR-NONE (five fields all NULL or all non-NULL), so `reverse_head` can never
    see a half-written binding classified as `dispatched`.
- [x] **Binding review round 2 (SP 71/71).**
  - **High** — replay compares the actual `reverse_input_json` CONTENT (not the
    caller-asserted, DB-unverified hash) and `already_requested` returns the full
    persisted binding incl. the input JSON, so a right-hash/wrong-json pair is
    rejected (`binding_conflict`) and the runner dispatches the durable value.
  - **Medium** — the all-or-none CHECK now also enforces VALIDITY on the complete
    tuple (16-byte id, non-empty name/hash, schema_version ≥ 1).
- [x] **Coverage review (SP 79/79 + host E2E).**
  - **Medium** — added `live_reversal_test.drift` (registered in the e2e list): a
    host E2E that EXECUTES all five reversal host methods against live MariaDB, so
    SP-name/signature/outcome-decode errors are caught at runtime, not just at
    compile. Reaches `begin_reversal {NotFound, Reversed, AlreadyBegun(state),
    TriggerMismatch}` and `reverse_head {NoneActive, Pending(seq,name,payload)}`;
    request/settle/block executed via fence_lost / NotRequested / NotFound. (The
    dispatched/requested/settled field decodes need a reversing+checkpoint state,
    not host-constructible until multi-op forward — covered at integration.)
  - **Medium** — reversing-direction `dispatch_defer` now tested (state stays
    reversing, lease clears, deadline persists).
  - **Medium** — `reverse_head_dispatched` now asserts `reverse_input_json`.
  - **Low** — conflict + CHECK tests split one-field-each (id/name/version/input/
    hash conflicts; empty-name/empty-hash/bad-version/short-id validity).
- [x] **Coverage review round 2.**
  - **Medium** — POSITIVE host decodes now covered. Added a test-only fixture proc
    `sp_mf_test_seed_reversing` (under `db/tests/`, loaded by the gate via
    `db-load-test-fixtures`, never in the production schema apply) that seeds a
    claimable reversing+checkpoint workflow. `live_reversal_test` now claims it and
    drives the REAL `reverse_request` → `Requested`, `reverse_head` →
    `Pending`(full payload) / `Dispatched`(full binding), `reverse_settle` →
    `Reversed`, `reverse_block` → `Blocked` — so field-name/variant-mapping defects
    are caught, not just SP names. (Raw `mariadb.rpc` used to call the seed proc.)
  - **Medium** — `reverse_head` pending payload asserted as the COMPLETE document
    (SP + host).
  - **Low** — reversing-defer deadline asserted by exact datetime equality.
  - **Low** — `live_reversal_test` requires `CreateOutcome::Created` (no `Exists`
    masking stale-state reuse), matching `live_lease_test`.
- [x] **Coverage review round 3.**
  - **Medium** — `db-load-test-fixtures` is now a lock-acquiring PUBLIC wrapper +
    private `_db-load-test-fixtures` (the load); `_test-locked` calls the private
    one (already under the shared lock — flocker is not re-entrant). A direct
    public invocation can no longer overlap another checkout's tests.
  - **Low** — host-test payloads asserted as EXACT documents via
    `json.encode_compact` against a parsed expected (Pending `{"reservation":"r2"}`,
    Dispatched input `{"undo":true}`, with `undo` also checked as boolean true).
- [x] **Runner reverse loop (compiles).** On claiming a `reversing(2)` workflow
  the runner branches to `_run_reversal`: reads the authoritative `reverse_head`
  (not the continuation) → on `pending`, looks up the manual-IR compensation
  binding (`operations[].compensation = {operation, schema_version}`), uses the
  forward checkpoint payload as the reverse input, persists via `reverse_request`,
  resolves the pinned reverse contract, and dispatches via the GENERIC
  `_classify_dispatch`; on `dispatched`, re-derives the id + reconciles. Result →
  `reverse_settle` (Reversed=terminal / Reversing=descend), Pending→`defer_dispatch`,
  Rejected→`reverse_block`. New: `CompensationBinding`/`CompStep` types,
  `_reverse_id` (distinct id space), `_compensation_for`, `_reverse_block`,
  `_compensate`; `_validate_operations` validates optional compensation bindings.
  (Drift quirk: variant match binds fields BY NAME — see memory.)
- [x] **Reverse-loop review round 1 (compiles).**
  - **High** — Dispatched recovery now DECODES + uses the durable persisted
    `reverse_invocation_id` (via `_hex_to_bytes`) for dispatch + settle, never a
    re-derived id.
  - **High** — a missing compensation binding (pre-dispatch resolution failure) now
    durably DEFERS (`_defer_dispatch`, "no_compensation_binding") instead of calling
    `reverse_block`, which would return `not_requested` and strand the lease.
  - **Medium** — `_reverse_block` handles `EventTimeSkew` → `_defer` to the supplied
    deadline (was reporting failure with the lease held).
  - **Medium** — `_validate_operations` rejects a compensation whose pinned
    `schema_version` ≠ the referenced operation's registered version (startup, not
    runtime defer).
  - **Low** — the classified participant rejection reason is persisted as
    blocked-resolution evidence (was overwritten with "compensation_rejected").
- [x] **End-to-end proof — added reversal integration coverage (8 assertions).**
  Stub gained a `release` compensation op (+ a `_fault.reject` definite-400
  injector); the runner config carries the manual-IR compensation binding
  (`reserve` → `release`). Seeded reversing fixtures (a0..05–0a, new checkpoint
  CSV) prove, through the REAL runner reverse loop dispatching `release` via the
  generic dispatcher:
  - `reverse_to_reversed` — one active checkpoint → compensation → `reversed`;
  - `reverse_terminal_idempotent` — re-run is terminal, makes no new request;
  - `reverse_lost_ack` — compensation commits then drops the ack → GET reconcile →
    still `reversed`, with exec-count delta == 1 (effectively-once) and request
    delta ≥ 2 (the PUT-that-lost-the-ack + the GET reconcile);
  - `reverse_restart_recovery` — a CONSISTENT post-request seed (binding +
    `compensation_requested` event + `reverse:dispatched` continuation) with the
    participant op pre-submitted under the durable id → `reverse_head` dispatched
    → reconcile (exec-count delta == 0, no re-execution) → `reversed`;
  - `reverse_no_active_checkpoint` — inconsistency → durable defer;
  - `reverse_block_on_rejection` / `reverse_block_no_redispatch` — definite
    compensation rejection (400) → `blocked_resolution` (reverse direction), no
    Singular op created, and a blocked workflow does not redispatch on rerun;
  - `reverse_no_compensation_binding` — checkpoint op with no compensation
    binding → durable operational deferral (lease released), not a block.
- [x] **Reverse-loop review round 2 (integration 25/25).**
  - **Medium** — restart recovery is GET-first: `_compensate` takes a `recover`
    flag; the Dispatched branch reconciles via `_reconcile` (GET-first), never a
    blind re-PUT through `_classify_dispatch`. Stub gains a PUT-only `put_count`
    (`/debug/put-count`); `reverse_restart_recovery` now asserts put-delta == 0
    (real GET-first), not just exec-delta == 0 (participant idempotency).
  - **Medium** — WF7 seed is transition-faithful: the `compensation_requested`
    event carries the full proc payload (`reverse_invocation_id`,
    `reverse_operation`, `reverse_schema_version`) with a strictly-increasing
    timestamp; workflow `current_event_ts` matches.
  - **Medium** — the rejection case asserts DURABLE evidence via a read-only
    `_mdb()` helper (new `reverse_block_durable_state`): checkpoint
    `resolution_required(3)`, the `compensation_blocked` event reason, and the
    `blocked_resolution(3)`/reverse workflow state — plus the classified reason in
    the runner response.
  - **Low** — removed stale duplicated next-step text after "Sub-step A complete";
    integration README + counts updated to 25.
- [x] **Reverse-loop review round 3 (integration 25/25).**
  - **Medium** — WF7 checkpoint is now FULLY transition-faithful: `reverse_input_hash`
    is the exact runner-derived value `d932b54d…984f` (validated:
    `nameUUIDFromBytes` over the compact lex JSON `{"reservation":"r7"}` — matches
    WF5's runner-persisted hash for `r5`), not the placeholder `rh7`; checkpoint
    `updated_at` advanced to the request event ts (`00:00:01`).
  - **Low** — `reverse_restart_recovery` also asserts request-delta == 1, proving the
    recovery DID contact the participant (exactly one GET) — GET-first reconcile, not
    zero interaction.
  - **Low** — reconciled the milestone count: the earlier log entry no longer asserts
    a stale `24/24`; the round-2 entry carries the single authoritative `25/25`.

## Sub-step A COMPLETE (proc + host + runner loop, proven end-to-end).
Next: sub-step B (multi-checkpoint stack traversal end-to-end) → C (multi-op
forward IR) → D (forward-success → later-failure → reverse-order proof).

## Relevant roadmap
Step 2 of the revised §7 sequence (dispatcher ✓ → reversal → manual portable IR →
ScriptRegistry → parser). Reuses the generic dispatcher per direction.
