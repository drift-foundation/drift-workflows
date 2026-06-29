# Workflow Composition — Design Plan (rev 2: decision #1 resolved + K round-2 fixes)

Concrete answers to the 7 charter questions. **Decision #1 (transport) is resolved → internal durable host
API, async + awaited.** K's round-1 corrections (**[K1]…[K6]**) and round-2 findings are folded in.

## Terminology (use consistently)

- **workflow call** — a parent step that invokes a **child workflow** and awaits its terminal outcome as the
  result of that step.
- **child workflow** — the workflow instance created/reasserted by the call.
- **call operation** — the parent's **pending operation row / checkpoint** representing that child.
- **detached / spawned workflow** — a *future* fire-and-forget form (not in scope here).
- **callback workflow** — **avoid** (implies event-handler / out-of-band notification semantics). A workflow
  call is **async but awaited**, not a callback.

---

## 0. The model — a workflow call is an async *internal* operation

A workflow call is an **operation whose participant is the coordinator itself, via an in-process host API**
(not HTTP). It is **always async**, even for a trivial child: the call operation *submits/reasserts* the
child workflow durably, then the parent **awaits via the normal pending-operation mechanics** — lease
cleared, next wakeup driven by child-terminal notification or scheduled inspection. **No inline child
execution; no nested call stack.**

**A parent awaiting a child is NOT a new state and NOT `blocked_resolution`.** It is a `requested/pending`
**call operation** while the parent remains in **normal forward execution** — exactly like any async job.

**Honest reuse boundary.** Composition reuses the operation **spine** — node-addressed identity, the
dispatch/settle/checkpoint shape, the `{forward:{input,result}}` envelope, terminal-from-durable replay,
idempotent reassert. It does **not** reuse:
1. **Outcome→checkpoint rule** [K2]: only a *completed* child becomes a parent checkpoint; a *failed* child
   is a rejection (not checkpointed; unwind prior; never re-compensated). (§4)
2. **Reverse-child compensation → new transition T1** [K1]: a terminal completed child is not claimable; the
   default reverse-child mode needs `completed(4) → reversing(2)`. (§5)
3. **The #2 HTTP-404 reconcile budget** [K4]: internal calls have **no transport 404**; they need their own
   **child-liveness/wait policy** (§Liveness) — and it stays in the **async operation model, never an
   automatic parent block**.

**Blocked does NOT cascade up the call tree** [K3, revised]. If A→B→C and C hits an operator-resolution
condition, **only C** enters `blocked_resolution`. B stays **forward** with a pending call operation awaiting
C; A stays **forward** awaiting B. Inspect/reporting MAY *derive* "waiting on blocked descendant" for
display, but ancestors are **never durably converted to `blocked_resolution`.** (This deletes the round-1
"T2 parent-unblock" transition — there is no parent block to undo.)

## 0.1 Decision #1 — RESOLVED: internal durable host API (not HTTP loopback)

Chosen: **Option A, internal host/API.** Rationale: no loopback, no reentrancy/admission/drain surface, no
worker self-call; the parent simply holds a durable **call operation** and is woken by notification/poll.
Consequence: **#2's route-404 budget is not directly reused** — the internal path defines its own liveness
policy (§Liveness). "Participant-shaped" survives as *semantics* (submit/inspect/reverse), not as HTTP.

---

## 1. Syntax — how a `.mf` step makes a workflow call

`call <child>@<rev> { <input> }`, bindable with `let`. The child is a **deployed, pinned workflow**
registered by **name + script revision** — *workflow revision identity* (script/plan version + content_hash
+ arg/return contracts), **not** a participant `schema_version` [K5]:

```
flow {
  let auth = call authorize@1 { order_id: arg order_id, amount: arg amount }   // @1 = script revision
  let cap  = call capture@1   { auth_id: result auth.auth_id }                 // §3 data flow
  return { capture_id: result cap.capture_id }
}
```

Per-call policy override (else defaults, §4); declared compensating workflow (§5); fan-out (§6):

```
  let cap    = call capture@1 { auth_id: result auth.auth_id } on failed { fail "capture_failed" }
  let charge = call charge@1  { … } compensation refund@1
  fan line in arg lines key line.sku { call reserve@1 { sku: line.sku, qty: line.qty } }
```

> **Decision (K):** keyword set (`call` / `fan … key` / `on failed` / `compensation`).

---

## 2. Identity — child workflow id derivation

```
child_workflow_id = H(parent_workflow_id, parent_content_hash, node_id, item_key)
    item_key = ""            single call            item_key = <stable key>  fan-out element (never an index)
```

The parent's **op-id for the call operation IS the child id** (one derivation, dual role). Branch-distinct,
retry/resume-stable, per-item for fan-out, no occurrence index. Compensating-workflow id =
`H(…, node_id, item_key, "comp")`. Derived only at fresh dispatch; resume adopts the durably-stored id.
Fan-out list expr must be **durable** (`arg` / settled `result`) so keys re-derive on recovery.

---

## 3. Data flow — child result → later parent steps

A `completed` child settles the call operation with its **`return` value**, stored in the parent checkpoint;
later steps read `result name.path` — identical to an op result. Parent sees the **narrowed `return` type**,
not the outcome document. Child contract `{ arg-type, return-type, compensation? }` keyed by **script
revision + content_hash** [K5], validated parent↔child at **build**. A `fan` binds a keyed map
`item_key → return`.

---

## 4. Control flow — child terminal → parent

| child terminal | parent behavior |
|---|---|
| `completed` | **checkpoint** the call operation (compensable); settle with the `return`; continue forward |
| `failed` | **NOT checkpointed** [K2] — a rejection: prior parent checkpoints unwind, the failed child is **never re-compensated** (it owns its own unwind). Default → begin parent reversal. `on failed { … }` may convert the failed outcome into a **non-compensable data result** the parent reads/branches on — it must **not** create a reverse-child compensation checkpoint (round-3 #1). |
| `blocked` | **parent stays FORWARD** with the call operation **pending** [K3, revised] — *not* blocked. The parent waits (notification/poll); when an operator resolves the *child*, the child reaches terminal and the parent proceeds. Inspect may *display* "waiting on blocked descendant"; the parent row is never durably `blocked_resolution`. |

A checkpoint exists to be undone; only *completed* children carry forward state worth compensating.

---

## 5. Compensation — reverse-child (T1) vs compensating-workflow [K1]

Only a *completed* child is a parent checkpoint, so compensation applies only to completed children. The
parent never absorbs the child's internal checkpoints:

- **Reverse-child (default) — needs transition T1.** Re-open the *terminal completed* child into reversal:
  **`completed(4) → reversing(2)`, fenced.** Idempotency by current child state:
  - already `reversing` / `reversed` → **idempotent no-op** (the reopen already happened);
  - already `failed` → **return already-terminal, "no parent compensation to run"** [round-2 #3] — *not* a
    reopen: a failed child was never a parent checkpoint, so reverse-child must never act on it (this guards
    against accidental double-compensation);
  - `completed` → perform the `completed→reversing` reopen.
  Then the child runs its own reversal; child `reversed`/`failed` → parent `compensation_settled`; child
  reverse-`blocked` → the parent's call operation stays pending (the child's block is local, §4).
- **Compensating-workflow (`compensation refund@1`).** Start a **fresh** child (id `…,"comp"`) with the
  `{forward:{input,result}}` envelope. **No reopen → no T1.** Simpler, and correct when undo ≠ rewind.

> **Decision #2 (K):** reverse-child (needs T1) by default, or ship compensating-workflow / no-comp first and
> defer T1? This sizes slice 1.

---

## 6. Fan-out — stable item-keyed children, no occurrence indexes [K6]

`fan line in arg lines key line.sku { call reserve@1 { … } }` → IR node `NFanOut(list, elem, key, child)`.

- `list` must be **durable** (§2). Each element → keyed child `H(…, node_id, line.sku)` (business key, never
  the index). Adding/removing an item touches only that child.
- **Keys must be a canonical scalar type; duplicate keys are rejected BEFORE any child is dispatched** [K6]
  (else two items derive the same child id with different input — a silent collision). Canonicalize + dedup,
  fail closed, at the fan boundary.
- Await = barrier over all children (Decision: all-or-policy; partial-failure mapping; comp order).

---

## 7. Recovery — parent/child replay + idempotent reassert

Parent replay = `advance()` over pure control flow + settled call-operation results from durable checkpoints
→ **terminal replay needs no live child.** Re-dispatch reasserts the child (submit-if-absent-else-current
under the stable id; idempotent by `child_id` + input hash); fan-out reasserts the keyed set. A
child-unreachable/stuck case is bounded by the liveness policy below (not the HTTP-404 budget).

---

## Recursion protection (required)

A workflow call introduces a call **tree**; cycles must be impossible.

- **Static / deploy-time:** reject obvious workflow-call cycles in the registry where derivable (A calls B
  calls A).
- **Runtime:** every child submission carries **root id + ancestry (or ancestor set) + depth** metadata. A
  call **fails closed** if it would (a) re-enter an **ancestor** workflow id, or (b) exceed `max_call_depth`.
- A recursion failure is a **normal call rejection** → drives the parent's existing failure/reversal
  semantics (§4 `failed`). Not a new error surface.

## Liveness / wait policy for the internal call operation [round-2 #2]

The internal path replaces #2's route-404 budget with an explicit, **async** liveness policy — concretely:

- **Durable child-link state** on the parent's **call operation** row: `child_workflow_id`, `child_status`
  (`pending|completed|failed|blocked`), `first_requested_at`, `last_inspected_at`.
- **How the parent learns outcomes:** the **push is TERMINAL-ONLY** — on a terminal state the child marks
  the parent's call operation due + records the outcome (an SP write); the parent must *act* (settle/fail).
  A child's **`blocked` status is non-terminal and display-only**: it is surfaced by **scheduled inspection
  (poll)**, NOT the terminal push (round-3 #2) — the parent takes no action on a blocked child, it just keeps
  waiting (§4). Poll also backstops a missed terminal notification. (Decision: push-primary+poll vs poll-only.)
- **Optional stuck-child budget** (config, default off → wait indefinitely for an alive/blocked child),
  stored on the call-operation row (reuse the #2 budget column *shape*: `attempts / first_seen / last_seen`):
  - **advances** on each scheduled inspection that finds the child **non-terminal and not making progress**;
  - **a `blocked` child is NOT "stuck"** — it is alive awaiting an operator → it must **not** advance the
    budget (else a legitimately-blocked child times out the parent). (Sub-decision: pause vs exclude.)
  - **exhaustion rule** (mirrors #2): `elapsed ≥ max_child_wait_ms AND attempts ≥ min` → the **call operation
    FAILS** (definite failure → parent reversal, §4) — **never an automatic parent block.**
- This keeps a stuck/slow child inside the **async operation model**; only an *explicit, configured* timeout
  converts it to a call failure, and only the child's own conditions ever produce `blocked_resolution`.

## New durable transitions (the core of the first slices)

- **T1 — reverse-child reopen** [K1]: child `completed(4) → reversing(2)`, fenced; no-op if already
  reversing/reversed; **already-terminal-no-comp if failed** (round-2 #3). Only for the reverse-child default.
- *(Round-1 "T2 parent-unblock" is removed — blocked does not cascade, so there is no parent block to undo.)*
- **Notification SP** — child-terminal → mark parent call operation due + record outcome (the push side of
  the liveness policy).

## Durable state (sketch)

Reuse `tb_mf_operation` + `tb_mf_workflow_checkpoint` for the call operation, adding: `call_kind`
(`participant | child_workflow`), `child_workflow_id`, `child_script@rev`, `child_status`,
`compensation_workflow@rev?`, and the liveness columns. The child workflow row gains `parent_workflow_id`,
`parent_node_id`, `root_workflow_id`, `call_depth`. Parent states stay 1–7; T1 adds a `completed→reversing`
edge. **No new parent state for "waiting on child" or "waiting on blocked descendant."**

## Change surface

parser (`call`/`fan … key`/`on`/`compensation`) · ir (`NCallWorkflow`, `NFanOut`, contract + durable-list +
canonical-key validation; static cycle check) · host (in-process submit/inspect/reverse-child + the
notification SP) · runner (dispatch=submit, await=notify/poll, settle, T1, recursion guard) · db (the columns
+ ancestry + migration; T1 + notification SPs) · docs.

## Test plan (folds into implementation)

idempotent call across retry/resume · child as ordinary forward step · **terminal replay with no live child**
· **T1** reverse-child exactly once + **failed child NOT re-compensated** [K2] · compensating-workflow mode ·
**blocked child does NOT block the parent** (parent stays forward/pending; resolve child → parent proceeds)
[K3] · **no blocked cascade** in A→B→C · **recursion guard** (cycle + depth → call failure) · fan-out stable
keys + **duplicate-key rejection** [K6] · stuck-child budget → call failure (not block); blocked child does
not trip it · root `just test` green.

## Open decisions (1 resolved)

1. **[K4] Transport — RESOLVED: internal host API.**
2. **[K1] Compensation default + T1** — reverse-child-by-default (needs T1) vs comp-workflow/no-comp first.
   **Agreed bias (user):** slice 1 ships **no-comp / compensating-workflow first** to prove the async call
   spine **without T1**; reverse-child/T1 is a separate slice, pulled in only if reverse-child must be the
   default from day one. K to take this deeper (both slice shapes, biased as above).
3. **Liveness** — push-primary+poll-fallback vs poll-only; stuck-child budget semantics (pause vs exclude a
   blocked child; default off?).
4. **[K2] Failed-as-data** — confirm "not checkpointed / unwind prior / never re-compensate"; define the
   `on failed` typed union if supported.
5. Fan-out await (barrier-all vs policy; partial failure) + comp order.
6. Recursion: ancestor-set vs root+depth-only; `max_call_depth` default.
7. Syntax keywords.

## Recommended first slice (depends on #2)

A single (non-fan-out) **workflow call**, internal host API, async-awaited, proving the spine — identity →
submit → await (notify/poll) → settle → result data-flow → recovery → **recursion guard** — **plus one
compensation mode.** **Agreed: that mode is no-comp / compensating-workflow** → slice 1 avoids **T1** and stays small (prove the
async call spine first). **Reverse-child + T1 is a deliberate slice 2** (the `completed→reversing` reopen is
a separate durable transition); the stuck-child budget and fan-out are also slice 2+.
