# Slice 1c — reverse-child/T1: durable transition spec (design-to-implementation pass)

Status: **DRAFT v2, pre-implementation — review checkpoint.** No SQL/Drift code has been written
against this spec yet. Revised after a review round that found two real issues in v1 (the settle SP
could not safely be "reverse_noop verbatim," and v1 self-contradicted on whether T1 writes the parent
row) plus two tightening requests. See "Changes from v1" at the bottom for exactly what moved and why.

This spec assumes the reader has `DESIGN.md` §4/§5/§"New durable transitions"/§"Durable state" and
1b.1's landed schema/SPs/runner as background — it does not re-derive those, only extends them.

---

## 0. What already exists that 1c reuses unchanged

Read through 1b.1's landed code before drafting this, specifically to avoid inventing machinery that
already exists generically:

- **`sp_mf_call_inspect`** already reads the CHILD's `state`/`execution_direction`/
  `current_disposition`/`is_terminal`/`terminal_reason`/`workflow_return_json` directly from
  `tb_mf_workflow` — it has no forward-only assumption baked in. **1c reuses it unchanged** as the
  parent's "await child compensation" poll, exactly as it's already reused as the parent's "await
  child forward completion" poll. No new inspect SP is needed.
- **The generic claim/resume/reverse machinery** (`sp_mf_workflow_claim`, `_run_reversal`,
  `sp_mf_checkpoint_reverse_head`, etc.) already treats every workflow uniformly by `state`. Once a
  child is flipped to `reversing(2)`, it is *just an ordinary reversing workflow* — its own recovery,
  claim, and reverse-loop code is **completely unchanged** by 1c. This is the main simplifying
  property of this design: T1 does the flip; everything downstream is code that already exists.
- **`_run_forward`'s `NeedCall` arm** (submit-then-await pattern: idempotent `call_submit` every
  pass, then a pure `call_inspect` read, branch on `is_terminal`) is the direct template for the new
  reverse-side "request-then-await" pattern below.
- **Participant checkpoint reversal is completely unchanged by 1c and does NOT "no-op."** A
  participant checkpoint with no declared compensation binding in deployment config
  (`_compensation_for` returns `None`) defers forever on `no_compensation_binding` — an operator must
  fix the binding; the workflow is never silently treated as compensated. This is pre-existing 1b.1
  behavior, not something 1c changes or weakens. The "no-op if it has nothing to undo" language in
  DESIGN.md §5 describes **call-kind checkpoints only** (see §2 below) — a child workflow's own
  checkpoint stack may legitimately have nothing requiring dispatch (e.g., all its own checkpoints are
  themselves call-kind and its own children no-op in turn), but that is never true for a participant
  checkpoint, which always needs an explicit binding or it strands by design. §6's nested test spells
  this distinction out explicitly so it isn't misread as "1c makes stranding optional."

## 1. State transition: `completed(4) -> reversing(2)` reopen (T1)

Preconditions and idempotency are keyed on the **child's current state**, read under `FOR UPDATE`
inside the transaction:

| child state at T1 time | outcome | mutates anything? |
|---|---|---|
| `completed(4)` | **reopen** (this section) | yes — child row + child event + parent event (§2) |
| `reversing(2)` / `blocked_resolution(3)` / `reversed(5)` / `resolved_exception(6)` | idempotent no-op — the reopen already happened at some earlier point (crash/retry) | no |
| `failed(7)` | **diagnostic inconsistency, not a valid branch** — see below (changed from v1) | no |

**`failed(7)` is now treated as corruption evidence, not a benign outcome (review finding #3).** A
call checkpoint only ever exists for a child that was `completed(4)` at checkpoint-creation time
(DESIGN.md §4: `failed` is never checkpointed). Traced the state machine: `failed(7)` is set *only* by
`begin_reversal`'s trivial "no active checkpoint at reversal start" branch, itself reachable only from
`forward(1)`. A T1-reopened child starts at `reversing(2)` directly (never re-enters
`begin_reversal`), and — per the invariant below — always has a non-NULL top checkpoint, so it can
never reach `failed(7)` through T1's own path either. If T1 (or settle, §2) nonetheless finds a
checkpoint's child in `failed(7)`, that means the checkpoint/child pairing is corrupt (a failed child
should never have become a parent checkpoint in the first place) — **settling or reopening as if this
were a normal case would hide that corruption.** T1 returns a distinct diagnostic outcome
(`child_state_inconsistent`, carrying the child's actual state) and performs no write; the runner
aborts with a diagnostic reason rather than proceeding. I still believe this branch is unreachable
under the current state machine — flagging the reasoning explicitly so a reviewer can confirm or
correct it, since if there's a path I'm missing, this branch is load-bearing, not decorative. (This
supersedes v1's "already-terminal-no-comp" framing, which treated it as an ordinary skip case.)

**The reopen write** (only on `completed(4)`), one atomic transaction — see §2 for the full
transaction shape, which now includes a parent-side write too:

```
tb_mf_workflow (child):
  state                 = 2 (reversing)
  execution_direction    = 2 (reverse)
  current_disposition    = 2 (failed)               -- confirmed OK on review, no new disposition code for MVP
  continuation            = {"pos":"reverse","seq":<child's own top active checkpoint seq>}
  fencing_token           = fencing_token + 1        -- "direct intervention" bump, tb_mf_workflow.sql's own convention
  terminal_reason         = 'parent_compensation'    -- confirmed OK on review
  next_attempt_at          = arg_event_ts            -- immediately claimable; T1 IS the wake, no separate notify call needed
  current_event_seq/ts     = advanced, child-side event appended (§2)
  lease_owner/expires_at  = untouched (already NULL on a completed(4) row; T1 does not claim)
```

- **The "top active checkpoint is never NULL for a `completed(4)` reopen" invariant.** Every
  completed workflow settled at least one operation (`plan_length >= 1`, "a workflow must execute at
  least one operation"), and `sp_mf_operation_settle` always creates a checkpoint in the same
  transaction as the settle ("no effects without Checkpoint"). So a `completed(4)` child always has
  >= 1 active checkpoint. If the top-seq lookup nonetheless returns NULL, that's a **durable
  inconsistency**, not a valid trivial-unwind case (unlike `begin_reversal`'s own NULL-top-seq
  branch, which is a legitimate forward-failure-with-nothing-to-compensate case) — T1 rejects with a
  structured outcome (`child_no_active_checkpoint`) and never silently proceeds, matching this
  codebase's existing "defer with a diagnostic, never synthesize" discipline.

## 2. Fenced/idempotent SP semantics

Two new SPs, named per review (confirmed): `sp_mf_checkpoint_reverse_child_reopen` (DESIGN.md's
informal "T1") and `sp_mf_checkpoint_reverse_child_settle`. The old `sp_mf_checkpoint_reverse_noop` is
retired — **its body is NOT reused verbatim** (review finding #1); the settle SP needs genuinely new
logic the old one never had.

### `sp_mf_checkpoint_reverse_child_reopen` ("T1") — now a two-row write (review finding #2)

**v1 said "the parent's row is never written by this SP," then separately proposed a parent-side
audit event — a real contradiction, since appending an event requires advancing
`current_event_seq`/`current_event_ts`, which is a write. Fixed by keeping the parent event (parity
with the participant path's `compensation_requested`) and making T1 an explicit two-row transaction.**
There is direct precedent for this shape in this exact codebase: `sp_mf_call_submit` is fenced on the
parent but also creates the child's entire row bundle (including the child's own "created" event) in
the same transaction. T1 is the same shape, writing an *existing* child instead of creating one.

```
IN arg_workflow_id     -- the PARENT
IN arg_executor
IN arg_fencing_token   -- the PARENT's fence
IN arg_seq             -- the parent's checkpoint seq (must be call_kind=2)
IN arg_event_ts
```

Phase order (mirrors this codebase's established convention: idempotent-check before fence, fence
before order, order before time-discipline, time-discipline before the single write phase):

1. Arg-shape SIGNALs.
2. Lock+read the PARENT row (`FOR UPDATE`) — `not_found` if missing.
3. Lock+read the PARENT's checkpoint at `arg_seq` — `checkpoint_not_found` if missing; type-guard its
   operation is `call_kind=2` (`not_call_checkpoint` otherwise) — structural, checked before any state
   machine logic, same as `reverse_noop`'s existing type guard.
4. Resolve `child_workflow_id` via the `tb_mf_call` sidecar (same join `sp_mf_call_inspect` already
   does), lock the child row (`FOR UPDATE`).
5. **Idempotent-replay / diagnostic check on child state (lease-independent, before the parent
   fence — same ordering `reverse_noop`'s own `already_reversed` check uses today):**
   - state IN (2,3,5,6) -> `already_reopened`, **no write at all** (neither row) — an
     already-committed T1 must not re-append either event, matching
     `tb_mf_workflow_event`'s own documented replay discipline ("a replayed command returns its
     recorded outcome and appends nothing").
   - state == 7 -> `child_state_inconsistent` (carries the child's state code), **no write** — see §1.
   - state == 4 -> proceed to the write path below.
6. **Fence check on the PARENT** (lease_owner/fencing_token/state=`reversing(2)`) -> `fence_lost`.
7. **Reverse order** (`arg_seq` == parent's own current top active checkpoint) -> `out_of_order`.
8. **Child's own top-active-checkpoint lookup** (for the child's new continuation) ->
   `child_no_active_checkpoint` if NULL (§1's invariant violation).
9. **Time discipline against BOTH timelines** (review finding #2's explicit ask): `arg_event_ts` must
   be strictly greater than both the parent's `current_event_ts` AND the child's
   `current_event_ts` -> `event_time_skew`, with `defer_until` computed from the *later* of the two
   (`GREATEST(parent_event_ts, child_event_ts) + margin`) so the retry clears both clocks.
10. **Write phase (both rows, one transaction):**
    - Parent: advance `current_event_seq`/`current_event_ts`; append event `kind='compensation_requested'`
      (same kind name as the participant path, for parity — payload carries `seq` +
      `child_workflow_id`, distinct from the participant path's `reverse_invocation_id`-shaped
      payload, since there is no dispatch/contract binding here). **The parent's own `state` /
      `continuation` are NOT touched** — the parent is still "at" this checkpoint per its existing
      reverse cursor; only its audit trail and event-seq counter advance.
    - Child: perform the reopen (§1's write block) + advance `current_event_seq`/`current_event_ts`;
      append event `kind='compensation_requested_by_parent'`, payload carries `parent_workflow_id` +
      the parent's `operation_seq` (§7 — correlation).

**Lock ordering.** Always parent-row-then-child-row, in both this SP and settle below. No existing
procedure locks a child's own `tb_mf_workflow` row before its parent's in a way that could conflict
(`sp_mf_child_terminal_notify` deliberately never locks the child's own row in the same transaction as
the parent's, precisely to avoid this class of risk — see its own header comment). Stating the
discipline explicitly here since T1/settle are the first procedures in this codebase to intentionally
lock two distinct workflow rows in one transaction.

**Why still no lease/fence check on the child side, despite now writing to it.** A `completed(4)`
child has `lease_owner IS NULL` (cleared on every terminal transition throughout this codebase) —
there is no lease to fence against, and nothing else in the system ever writes to a terminal
`completed(4)` row. Concurrency safety comes from: (a) the parent-side fence, which ensures at most
one worker can even reach step 4 with a *fresh* (state=4) child to mutate, and (b) the child-row
`FOR UPDATE` lock + idempotent branch inside the same transaction, which makes a retried call safe
regardless. The parent-side write in step 10 is gated by the exact same parent fence as the child-side
write — they commit atomically together or not at all.

Outcomes: `reopened` (fresh, both rows written) / `already_reopened` (idempotent, no write) /
`child_state_inconsistent` (state 7, no write, diagnostic) / `child_no_active_checkpoint` /
`fence_lost` / `out_of_order` / `not_call_checkpoint` / `checkpoint_not_found` / `not_found` /
`event_time_skew`.

### `sp_mf_checkpoint_reverse_child_settle` — genuinely new logic (review finding #1)

**v1 proposed this as `reverse_noop` renamed verbatim. That's unsafe: it would flip the parent's
checkpoint to reversed based purely on the parent's own reverse-order/fence state, without ever
checking that the child actually finished compensating — a runner bug (e.g. calling settle right
after a reopen, before polling) could settle the parent while the child is still `reversing(2)` or
stuck in `blocked_resolution(3)`.** Fixed: this SP now reads and verifies the child's state itself,
inside the same transaction, as the authoritative precondition for the write — it does not trust the
caller's own `call_inspect` read (which happened in a separate, earlier transaction and could be
stale).

```
IN arg_workflow_id, arg_executor, arg_fencing_token, arg_seq, arg_event_ts   -- same shape as before
```

Phase order:

1. Arg-shape SIGNALs.
2. Lock+read the PARENT row -> `not_found`.
3. Lock+read the PARENT's checkpoint at `arg_seq` -> `checkpoint_not_found`; type-guard
   `call_kind=2` -> `not_call_checkpoint`.
4. Idempotent-replay (lease-independent, before fence, same ordering as today's `reverse_noop`):
   checkpoint `reversal_state == 2` -> `already_reversed`, no write. `reversal_state` not in `(1,2)`
   -> `checkpoint_not_settleable`.
5. Fence check on the PARENT (lease/fence/state=`reversing(2)`) -> `fence_lost`.
6. Reverse order (`arg_seq` == parent's current top active checkpoint) -> `out_of_order`.
7. **Resolve `child_workflow_id` via the sidecar, lock+read the child row, and require its state be
   in `(5,6)` (reversed / resolved_exception) before proceeding (review finding #1's core ask):**
   - state IN (5,6) -> proceed to the write.
   - state IN (2,3) -> `child_not_terminal` (compensation genuinely still in flight or stuck — this
     is the *expected*, non-error shape when the runner calls settle too early or races its own poll;
     the caller should defer, not abort).
   - state IN (4,7) -> `child_not_compensated` (carries the child's actual state) — structurally
     wrong: state 4 means T1 was somehow never run (or its write was rolled back independently of
     this transaction, which shouldn't be possible given they're separate committed transactions, but
     is still worth surfacing rather than assuming); state 7 is the same corruption case as §1. Either
     way, this is diagnostic, not a normal polling outcome — the caller aborts rather than retries
     forever.
8. Time discipline (parent's own `current_event_ts` only — this SP does not write the child's event
   stream, only reads it for verification) -> `event_time_skew`.
9. Write phase: flip the checkpoint `reversal_state` 1->2; descend to the next active checkpoint
   (stay `reversing(2)`) or, if none remain, reach the parent's own `reversed(5)` terminal (lease
   cleared) — identical mechanics to what `reverse_noop` already had, since that part was never wrong.
   Append the parent's own event `kind='compensation_settled'` (same kind name as the participant
   path's existing settle event, for parity).

Outcomes: `reversed` (terminal) / `reversing` (descend) / `already_reversed` / `child_not_terminal`
(new) / `child_not_compensated` (new, carries child state) / `checkpoint_not_settleable` /
`fence_lost` / `out_of_order` / `not_call_checkpoint` / `checkpoint_not_found` / `not_found` /
`event_time_skew`.

**`sp_mf_checkpoint_reverse_noop` is retired**, not left as dead code — once every call_kind=2
checkpoint goes through this pair unconditionally (DESIGN.md: "Slice 1c MVP has one behavior"),
nothing calls it. Its file/SP name is removed; the reusable *mechanics* (checkpoint flip,
descend-or-terminal) live on inside `_child_settle`, but the entry point itself does not survive as
a separate callable — retaining an unreachable "noop" alongside a settle SP that now does real
verification would be confusing, not merely redundant.

## 3. How parent reversal invokes it from a call checkpoint

Replaces the runner's current `_run_reversal` `call_kind == 2` branch (`runner.drift:3372-3400`),
which today unconditionally calls `checkpoint_reverse_noop`. New shape, mirroring `NeedCall`'s
submit-then-await pattern — the settle SP's own child-state check (§2) is the authority; the runner's
`call_inspect` read is only used to decide *whether to attempt* settle (avoiding a wasted round-trip
when the child is obviously still in flight), never to bypass the settle SP's own verification:

```
if call_kind == 2 {
    // (a) idempotent "ask the child to compensate" -- safe on every pass, fresh or resumed,
    // exactly like call_submit is called unconditionally on every NeedCall pass.
    match host.checkpoint_reverse_child_reopen(workflow_id, fencing_token, seq, &t) {
        Reopened | AlreadyReopened => {},   // proceed to (b)
        ChildStateInconsistent(child_state) => {
            return Outcome::ReverseAborted(reason = "checkpoint_reverse_child_state_inconsistent", code = 8);
        },
        ChildNoActiveCheckpoint => { return Outcome::ReverseAborted(reason = "...", code = 8); },
        FenceLost => { return Outcome::ReverseAborted(...); },
        OutOfOrder(top_seq) => { return Outcome::ReverseAborted(...); },
        EventTimeSkew(defer_until) => { return _defer(...); },
        default => { return Outcome::ReverseAborted(...); }   // NotCallCheckpoint / NotFound / CheckpointNotFound
    }

    // (b) pure read -- REUSED, unchanged. Same call used by NeedCall's forward await. Used here only
    // to decide whether it's worth attempting settle; the settle SP re-verifies independently.
    match host.call_inspect(workflow_id, seq) {
        Found(child_workflow_id, child_status, state, direction, disposition, is_terminal, terminal_reason, _) => {
            if !is_terminal {
                // reversing(2) or blocked_resolution(3): child compensation in flight or stuck.
                // NO CASCADE (DESIGN.md): the parent's own state stays reversing(2), never
                // blocked_resolution. Defer normally, same shape as forward's _defer_pending.
                if state == STATE_BLOCKED {
                    // same best-effort hint refresh already used on the forward side
                }
                return _defer_reverse_pending(host, workflow_id, fencing_token, &child_workflow_id, admission);
            }
            // is_terminal: attempt settle regardless of WHICH terminal state -- the settle SP's own
            // child-state check (§2) is the single source of truth for whether this is a valid
            // settle, not this runner-side branch.
            match host.checkpoint_reverse_child_settle(workflow_id, fencing_token, seq, &t) {
                Reversed(terminal_reason) => { return Outcome::TerminalFailure(reason = ..., compensated = true); },
                Reversing(next_seq) => {},   // descend -> re-read head
                AlreadyReversed => {},
                ChildNotTerminal => {
                    // race: our own call_inspect read is stale relative to the settle SP's own
                    // (later) read, or the child regressed somehow. Defer and re-poll -- not an error.
                    return _defer_reverse_pending(host, workflow_id, fencing_token, &child_workflow_id, admission);
                },
                ChildNotCompensated(child_state) => {
                    return Outcome::ReverseAborted(reason = "checkpoint_reverse_child_not_compensated", code = 8);
                },
                OutOfOrder | FenceLost | EventTimeSkew | default => { /* same shape as today's error handling */ }
            }
            continue;
        }
    }
}
```

- **No cascade, confirmed symmetric with the forward side.** DESIGN.md's own text ("child
  compensation blocked -> the parent's call operation stays pending") resolves what I initially
  expected might need a new `reverse_block`-analog SP: it doesn't. A stuck child compensation simply
  means the parent's reverse loop keeps deferring/polling that checkpoint — no new durable
  "blocked-during-reverse" state is needed on the parent, matching `blocked_resolution` never
  cascading on the forward side either.
- **`_defer_reverse_pending` (new, small):** the reverse-side sibling of `_defer_pending` — same
  shape (carries `child_workflow_id` so `Outcome::Pending`'s existing rendering surfaces which child
  the reversal is waiting on), but the workflow stays `reversing(2)` rather than `forward(1)`. This
  is plumbing, not a design decision — flagging only so it isn't missed in the build checklist.

## 4. Terminal/blocked child outcomes during compensation

| child terminal reached | parent action |
|---|---|
| `reversed(5)` | normal success — full unwind. Settle the parent's checkpoint (§2/§3), descend or reach the parent's own terminal. |
| `resolved_exception(6)` | an authorized (human) resolution disposed of the child's own blocked checkpoint. The settle SP's own CONTROL FLOW is **indistinguishable from `reversed(5)`** — settle identically, matching "the parent must never enumerate... the child's internal compensations." (The `compensation_settled` audit event DOES record which — `child_state` 5 or 6 — as a passive correlation field, per §7's observability requirement; that is audit trail, not a decision the parent's logic makes differently, so it does not weaken the invariant above.) |
| `blocked_resolution(3)` (non-terminal) | **no cascade** — parent defers, stays `reversing(2)`. An operator resolves the *child* directly; the parent's next poll then observes the child terminal and proceeds. Unbounded by 1c (no stuck-child budget yet — that's slice 2, same as the forward side). |
| `failed(7)` | **diagnostic inconsistency (changed from v1) — never settled as if compensated.** Both the reopen and settle SPs treat this as `child_state_inconsistent`/`child_not_compensated`; the runner aborts with a diagnostic reason. A failed child should never have been a parent checkpoint at all, so reaching this state is corruption evidence, not a benign "nothing to do" case. |
| `completed(4)` (still, after settle is attempted) | durable inconsistency — T1 runs every pass and should have already reopened it; the settle SP's own `child_not_compensated` outcome catches this; abort with a diagnostic reason, never silently retry forever. |

## 5. Replay behavior

- **T1 is called unconditionally on every pass** (fresh or resumed) — same philosophy as
  `call_submit`. No "was T1 already sent" bookkeeping is needed on the parent side; T1's own
  idempotency (keyed on the child's *current* state, not on a persisted "was this dispatched" flag)
  makes re-calling it safe and correct every time, and a true replay (§2 step 5) writes nothing at
  all — neither the parent's nor the child's event stream advances twice.
- **Crash after T1 commits, before the parent's settle commits:** on resume, the parent's reverse
  loop re-enters the same `call_kind==2` branch, re-calls T1 (idempotent `already_reopened`, since
  the child is already past `completed(4)`), re-calls `call_inspect` (reads the child's *current*,
  possibly-now-terminal state), and proceeds correctly. No new recovery code path — this is the same
  "idempotent-every-pass" property that already makes 1b.1's forward recovery work.
- **The child's own resume/recovery is completely unchanged.** Once T1 flips it to `reversing(2)`,
  it is claimed, resumed, and driven exactly like any other reversing workflow via the existing
  generic machinery. This is the main simplifying property of reverse-child/T1 as a mechanism: no
  new recovery logic is needed *anywhere* on the child side.
- **Fencing-token bump on reopen** means a stale holder of the child's (already-cleared) lease can't
  exist to be confused by this — defense-in-depth consistent with every other durable-intervention
  transition in this codebase, not something a real recovery path depends on.

## 6. Required tests

- **Nested A -> B -> C compensation (the acceptance test DESIGN.md calls out).** A calls B calls C.
  A's reversal reaches its call-to-B checkpoint -> T1 reopens B -> B is now an ordinary reversing
  workflow -> B's *own* reverse loop reaches its call-to-C checkpoint -> T1 (the *same* mechanism,
  recursively, since B's reverse loop is just the generic machinery) reopens C -> **C's own
  checkpoint(s), if call-kind, settle without needing a binding; if C also has a PARTICIPANT
  checkpoint with no compensation binding declared, C instead defers forever on
  `no_compensation_binding` (unchanged 1b.1 behavior, §0) — the test should cover both a
  "pure call-kind chain, fully unwinds" case and a "participant checkpoint present, correctly strands
  pending an operator fix" case, so the two are never conflated.** For the fully-unwinding case: C
  reaches `reversed(5)` -> B's checkpoint settles -> B reaches `reversed(5)` -> A's checkpoint
  settles -> A reaches `reversed(5)` (or continues to any other checkpoint A itself has). Assert the
  full chain's final states, and that each level's checkpoint event only references its *own* child,
  never a grandchild's identifiers.
- **No parent enumeration of child internals (assertion test).** Query the parent's own
  `tb_mf_workflow_event`/`tb_mf_workflow_checkpoint` rows for every T1-related kind and assert they
  carry only `child_workflow_id` + a high-level outcome — never a child-internal
  operation_name/payload/checkpoint-seq. This is a DB-level structural assertion, not just a code
  read, so a future refactor can't silently regress the encapsulation invariant.
- **Exactly-once compensation request.** Call T1 (via repeated resumed parent drives, simulating
  retry) multiple times against the same child; assert the child's `completed(4)->reversing(2)`
  transition happened exactly once — e.g. count the T1-triggered event kind on the child == 1 and the
  parent's `compensation_requested` event kind at this seq == 1, regardless of how many times the
  parent's reverse loop re-enters this checkpoint.
- **Settle refuses a non-terminal or wrongly-terminal child (direct regression for finding #1).**
  Call `checkpoint_reverse_child_settle` directly (bypassing the runner's own `call_inspect` gate)
  against a checkpoint whose child is still `reversing(2)` -> assert `child_not_terminal`, no write.
  Against a child somehow left at `completed(4)` -> assert `child_not_compensated`, no write. This is
  the test that would have caught v1's unsafe verbatim-reuse proposal.
- **`failed(7)` child is diagnosed, never silently settled (direct regression for finding #3).**
  Construct (via direct DB manipulation in the test, since this state is believed unreachable through
  normal operation) a call checkpoint whose child is `failed(7)`; assert both
  `checkpoint_reverse_child_reopen` and `checkpoint_reverse_child_settle` return their diagnostic
  outcomes and neither settles the parent's checkpoint.
- **Failed child never re-compensated (forward-path failure, distinct from the above).** A child
  that fails on the *forward* path (never reaches `completed`) is never checkpointed at all (§4 of
  DESIGN.md, already covered by 1b.1's own tests via `CallRejected` -> `_begin_reversal_unwind`) —
  confirm no call checkpoint exists for it, so `reverse_head` never surfaces it and T1 is never
  invoked for it. This is mostly a regression pin on already-existing 1b.1 behavior, re-asserted in
  the 1c suite for completeness.
- **Blocked child compensation -> parent stays pending, not blocked.** Force the child's own
  compensation into `blocked_resolution(3)` (e.g. its own compensation dispatch fails nonretryably);
  assert the parent's `call_inspect` observes `is_terminal=0`/`state=STATE_BLOCKED`, the parent
  defers (`Outcome::Pending`), and the parent's own `tb_mf_workflow.state` stays `reversing(2)` —
  never durably `blocked_resolution(3)` itself. Direct reverse-side mirror of 1b.1's forward-side
  "blocked child does NOT block the parent" test.
- **Recovery of in-flight child compensation.** Crash/resume between T1's commit and the parent's
  settle commit (§5) — confirm resume completes correctly with no duplicate side effects.
- **Fence-before-mutation ordering.** Two racing resumed workers only one of which holds the
  parent's current fence: confirm the fenced-out worker gets `fence_lost` from
  `checkpoint_reverse_child_reopen` *before* either row is touched.
- **Dual event-time-skew.** Construct a case where the child's own `current_event_ts` is *later* than
  the parent's (e.g. the child had other recent activity) and confirm T1's time-discipline check
  (§2 step 9) defers against the later of the two, not just the parent's own clock.

## 7. Observability/correlation (carried forward per PROGRESS.md's 1c requirement)

Per `work/workflow-composition/PROGRESS.md`'s "1c observability/correlation requirement": durable
events give the big-picture timeline, service logs give detailed execution evidence, and the two must
be joinable by shared, stable identifiers. Applied concretely to T1/settle:

- **The child's reopen event** (`compensation_requested_by_parent`) carries `parent_workflow_id` +
  the parent's `operation_seq` that triggered it — so `mfinspect inspect <child_id>` (or a future
  log-correlation pass) can answer "why did this workflow start reversing?" without needing the
  parent's id supplied externally.
- **The parent's own audit trail** gets matching events at both T1-reopen time
  (`compensation_requested`, carrying `seq` + `child_workflow_id`) and settle time
  (`compensation_settled`, same kind name as the existing participant-path settle event) — reusing
  the participant path's own kind names for parity, so the two compensation flows (participant vs.
  child) are structurally consistent in the audit trail, not just behaviorally.
- **Any new best-effort/service-log lines** the runner emits around T1 (mirroring the 1b.1 review
  round's `_log_best_effort` pattern) should carry the same identifier set: `workflow_id` (hex),
  `operation_seq`, `child_workflow_id` — so a log line and an `mfinspect`/DB-level event can be joined
  by a human or a future tool.
- **`mfinspect` itself needs no code changes for 1c**, provided the above field-naming conventions
  are followed: its existing `events`/`checkpoints`/`calls` output already surfaces every field
  generically (it dumps whatever columns/payload exist, unfiltered). This is an acceptance criterion
  on the new SPs' event payloads, not a claim that needs separate verification work — it falls out of
  `mfinspect`'s existing design if the naming above is followed.

## 8. Explicitly out of scope for this pass (unchanged from DESIGN.md)

- `compensation <wf>@<plan_version>` stays build-rejected (1a's existing behavior, untouched).
- No compensating-workflow mode, no mode selector.
- No stuck-child-during-compensation budget (slice 2, standalone, same as the forward side).
- Fan-out compensation ordering (slice 3).

---

## Changes from v1 (this review round)

1. **[High, fixed] `sp_mf_checkpoint_reverse_child_settle` is no longer "reverse_noop verbatim."** It
   now independently locks and verifies the child's state is `reversed(5)`/`resolved_exception(6)`
   before flipping the parent checkpoint, with new `child_not_terminal`/`child_not_compensated`
   outcomes. §2, §3, §6 updated; a dedicated regression test added.
2. **[High, fixed] T1 is now an explicit two-row (parent + child) write, not a child-only write with
   a contradictory parent-side event.** Kept the parent event, per the reviewer's own recommendation,
   for parity with the participant path's `compensation_requested`. Added dual event-time-skew
   checking (§2 step 9) and precedent-based justification (`sp_mf_call_submit` already writes
   parent+child in one transaction). §2, §5, §7 updated.
3. **[Medium, fixed] `failed(7)` is now a diagnostic/inconsistency outcome in both T1 and settle, not
   a soft "already-terminal-no-comp" success shape.** §1, §3, §4 updated; a dedicated regression test
   added (§6).
4. **[Medium, fixed] Tightened wording so "no-op if nothing to undo" is explicitly scoped to
   call-kind checkpoints only.** Added §0's explicit statement that participant checkpoints still
   strand on a missing compensation binding, unchanged by 1c; §6's nested test now explicitly covers
   both the pure-call-kind-chain case and the participant-checkpoint-strands case so they can't be
   conflated.
5. **Resolved on review, no longer open:** `current_disposition=failed(2)` reuse (confirmed OK, no new
   disposition code for MVP); `terminal_reason='parent_compensation'` (confirmed OK); SP names
   `sp_mf_checkpoint_reverse_child_reopen`/`_child_settle` (confirmed, retiring the old `noop` name
   entirely rather than keeping it as an unreachable alias).

No further open questions remain from this round. Implementation may begin once this revision is
accepted.
