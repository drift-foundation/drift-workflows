# Workflow Composition — Design Plan (rev 2: decision #1 resolved; K review rounds folded in)

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
dispatch/settle/checkpoint shape, the `{forward:{…}}` compensation envelope (full shape §5),
terminal-from-durable replay,
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

`call <child>@<plan_version> { <input> }`, bindable with `let`. **`@<plan_version>` is a semantic plan
version** (`major.minor.patch`, e.g. `@1.0.0` — the same `plan_version` vocabulary the top-level plan pin
uses), **not** an opaque alias and **not** a participant `schema_version` [K5]. The registry resolves
`(child_name, plan_version)` by **exact-match** to the child's pinned **`(content_hash, plan_length)`** —
identical to how a top-level workflow pins its plan (`tb_mf_workflow_plan`) — so the durable child identity is
the full **plan-identity key `(script_name, plan_version, content_hash)`** (§Recursion, §Durable state). The
child is a **deployed, pinned workflow** carrying `{arg-type, return-type, compensation?}` contracts:

```
flow {
  let auth = call authorize@1.0.0 { order_id: arg order_id, amount: arg amount }  // @1.0.0 = semantic plan_version
  let cap  = call capture@1.0.0   { auth_id: result auth.auth_id }                // §3 data flow
  return { capture_id: result cap.capture_id }
}
```

Per-call policy override (else defaults, §4); declared compensating workflow (§5); fan-out (§6):

```
  let cap    = call capture@1.0.0 { auth_id: result auth.auth_id } on failed { fail "capture_failed" }
  let charge = call charge@1.0.0  { … } compensation refund@1.0.0
  fan line in arg lines key line.sku { call reserve@1.0.0 { sku: line.sku, qty: line.qty } }
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
not the outcome document. Child contract `{ arg-type, return-type, compensation? }` keyed by the
**plan-identity key `(script_name, plan_version, content_hash)`** [K5] (resolved from `@<plan_version>` by
exact-match), validated parent↔child at **build**. A `fan` binds a keyed map
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
- **Compensating-workflow (`compensation refund@1.0.0`).** On reversal of the completed-child checkpoint,
  start a **fresh** child (id `…,"comp"`) with the **standard forward-context envelope the runner already
  builds** (`_comp_envelope`): **`{forward:{workflow_id, operation, operation_id, schema_version, input,
  result}}`**, where for a workflow call **`forward.operation_id = child_workflow_id` is the correlation
  key** (the exact child execution being undone), `forward.operation = child_script_name`,
  `forward.schema_version = CALL_OPERATION_SCHEMA_VERSION`, `forward.input` = the original call input, and
  `forward.result` = the child's `return` — optionally extended with the **child plan identity**
  (`child_script_name` / `child_plan_version` / `child_content_hash`) so the comp knows the exact child plan
  it undoes. The comp workflow itself is resolved at build and **pinned** at comp-submit by its **exact plan
  identity** (`comp_script_name` / `comp_plan_version` / `comp_content_hash`) — mirroring how
  `reverse_operation_name`+`reverse_schema_version` pin a participant compensation — so a later registry
  change cannot replay a different compensation. **No reopen → no T1.** Simpler, and correct when undo ≠
  rewind.

> **Decision #2 (K) — RESOLVED → slice plan.** Slice 1 ships **no-comp / compensating-workflow only** (no
> reverse-child, no T1). Reverse-child + T1 (`completed(4)→reversing(2)`) is **slice 2**.

---

## 6. Fan-out — stable item-keyed children, no occurrence indexes [K6]

`fan line in arg lines key line.sku { call reserve@1.0.0 { … } }` → IR node `NFanOut(list, elem, key, child)`.

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
- **Runtime:** the child row stores `parent_workflow_id` + `root_workflow_id` + `call_depth` (§Durable
  state); `sp_mf_call_submit` **reconstructs the ancestor set** by walking `parent_workflow_id` links and
  joining `tb_mf_workflow_plan`, collecting each ancestor's **plan-identity key `(script_name, plan_version,
  content_hash)`** — **exact plan identity**. (`script_name` is on `tb_mf_workflow`; `plan_version` +
  `content_hash` are on `tb_mf_workflow_plan`, so **no new column and no denormalized ancestor-set column**;
  the walk is bounded by `call_depth ≤ max_call_depth`, and plan identity is immutable so the read needs no
  lock.) A call **fails closed** if it would (a) re-enter an **ancestor plan identity** — the child's key is
  already in the reconstructed set — or (b) exceed `max_call_depth`. **Child ids are freshly derived per
  call** (§2), so an ancestor *instance-id* check can NOT catch a logical A→B→A cycle (the second A derives a
  brand-new id); the **plan-identity key** can. Slice 1 **hard-bans** ancestor re-entry by that key AND
  enforces `max_call_depth`. (The plan-identity ban is conservative — it forbids *all* same-plan re-entry,
  even legitimately-terminating recursion; that is the safe slice-1 choice.)
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
  waiting (§4). Poll also backstops a missed terminal notification. (**Slice 1: terminal push + poll
  fallback** — decided; slice 2 decides only the stuck-child timeout/budget.)
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
- **Notification SP** — **wake/stage-only**: child-terminal → mark parent call operation due + **stage** the
  child terminal outcome in call-specific columns (the push side of the liveness policy). It **never** settles
  the parent (`status`/`result_json`) or creates a checkpoint — the runner (via `call_inspect`) performs the
  single authoritative settle/reversal.

## Durable state (sketch)

Reuse `tb_mf_operation` + `tb_mf_workflow_checkpoint` for the call operation. **The op row keeps its existing
invariants unchanged:** `operation_id = child_workflow_id` (the op-id IS the child id — idempotent reassert
falls out of the existing `(operation_id, input_hash)` replay); **`operation_name = child_script_name`** (no prefix — `child_script_name` is already `varchar(128)`, so a
`"call:"` prefix could overflow; the authoritative discriminator is **`call_kind = child_workflow`** on the
op row, which the reverse/audit path reads before any compensation lookup, so a child call never mis-resolves
against a same-named participant op). `sp_mf_operation_settle` copies `operation_name` into the checkpoint, so
`forward.operation = child_script_name`. `input_json`/`input_hash` = the call input; `schema_version` stays
the **call-operation** schema version — a fixed constant **`CALL_OPERATION_SCHEMA_VERSION = 1`** (we do
**not** overload it with the child plan revision; `sp_mf_call_submit`'s idempotent replay checks it like any
pinned `schema_version`); `status`/`result_json` keep the `ck_mf_operation_status_result`
invariant — `requested(1)` while the child runs, `succeeded(2)` + `result_json = child return` **only** when
the runner settles a *completed* child. Added columns: `call_kind` (`participant | child_workflow`),
`child_workflow_id`, **child plan identity** `child_script_name` / `child_plan_version` / `child_content_hash`,
`child_status`, the **compensation plan identity** `comp_script_name` / `comp_plan_version` /
`comp_content_hash` (all NULL when no compensation — pinned exactly like the checkpoint's
`reverse_operation_name`+`reverse_schema_version`, so a registry change can't replay a different
compensation), and the child-terminal **stage** columns the notify SP writes (e.g. `child_return_json` /
terminal status). The child workflow row gains `parent_workflow_id`, `parent_node_id`, `root_workflow_id`,
`call_depth`. **Liveness / stuck-child budget columns are slice 2, not here.** Parent states stay 1–7; T1
(slice 2) adds a `completed→reversing` edge. **No new parent state for "waiting on child" or "waiting on
blocked descendant."**

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

## Decisions — resolved + slice assignments

1. **[K4] Transport — RESOLVED: internal host API** (§0.1).
2. **[K1] Compensation default + T1 — RESOLVED → slices.** Slice 1 = no-comp / compensating-workflow; slice 2
   = reverse-child + T1.
3. **Liveness — split.** The **await mechanism is DECIDED for slice 1: terminal push + poll fallback.** Slice
   2 decides only the **stuck-child timeout / budget behavior** (default off → wait indefinitely; pause vs
   exclude a blocked child; the exhaustion rule) and adds its columns. **No liveness-budget columns in slice
   1.**
4. **[K2] Failed-as-data — RESOLVED + sliced.** "not checkpointed / unwind prior / never re-compensate" is
   **slice 1**; the `on failed` typed union is **slice 3**.
5. **Fan-out await — slice 3** (barrier-all vs policy; partial failure; comp order).
6. **Recursion — RESOLVED:** ancestor **set** of plan-identity keys `(script_name, plan_version,
   content_hash)` + `max_call_depth` (root+depth-only bounds but can't *catch* a cycle). `max_call_depth`
   default is fixed at implementation.
7. **Syntax keywords — RESOLVED** (`call` / `fan … key` / `on failed` / `compensation`; slice 1 parses only
   `call` + `compensation`, rejecting the rest with a clear "not in this release").

## Slice plan (decided)

> **Slice 1 boundary — explicit.** Slice 1 proves **workflow calls as async, awaited operations.**
> **No inline child execution. No blocked cascade. No reverse-child reopen (T1).** Compensation in slice 1
> is **no-comp / compensating-workflow only** (a *fresh* comp child via the forward envelope — never a
> `completed→reversing` reopen).

- **Slice 1 — async workflow-call spine, no T1.** Single (non-fan-out) `call`, internal host API,
  async-awaited: identity → submit/reassert → await (terminal push + poll fallback) → settle + result
  data-flow → *child-failed = rejection → unwind prior* → *child-blocked = parent stays forward, no cascade*
  → recovery (replay-from-durable + idempotent reassert) → **recursion guard**. Compensation = **no-comp /
  compensating-workflow** (fresh comp child, no reopen).
- **Slice 2 — reverse-child + T1.** The `completed(4) → reversing(2)` reopen (fenced/idempotent;
  `failed` → already-terminal-no-comp), reverse-child as a mode (then decide whether it's the default),
  **plus the stuck-child liveness budget** (§Liveness).
- **Slice 3 — fan-out + failure-as-data.** `NFanOut` (canonical keys + dedup, barrier await + comp order)
  and `on failed`-as-data / the typed failure union.

## Slice 1 — build checklist (dependency order)

Concrete enough to start. Each stage lands with its own tests (tests are part of the slice, not a pre-phase).

**1. Grammar — `parser.drift`**
- [ ] Parse the step form `let <x> = call <child>@<plan_version> { <input-object> }` (also as a bare
      statement). `@<plan_version>` is a **semantic version token** `major.minor.patch` (e.g. `1.0.0`).
- [ ] Parse the optional `compensation <wf>@<plan_version>` clause on a call. **Do not** add `on failed` /
      `fan` / `key` yet (slice 2/3) — leave them unparsed so they fail with a clear "not in this release".
- [ ] AST: `CallStmt { binder?, child_name, child_plan_version, input_expr, compensation? }`.
- [ ] Tests: parse fixtures (with/without `compensation`, with/without binder); malformed `call` rejected.

**2. IR + validation — `ir.drift`**
- [ ] New node `NCallWorkflow { node_id, child_name, child_plan_version, input_expr, result_binder?, compensation_ref? }`;
      lower `CallStmt` → it.
- [ ] Bind the child's declared **`return` type** (from the registry) to the binder; `result <x>.path`
      resolves against it (reuse the EResult typing path).
- [ ] Validate at build: child `name@<plan_version>` resolves in the registry (exact-match → pinned
      `content_hash`); **input matches child `arg`-type**, downstream uses match child `return`-type;
      `compensation@<plan_version>` (if any) resolves and accepts the
      `{forward:{workflow_id,operation,operation_id,schema_version,input,result}}` envelope; input expr
      references only durable/in-scope values.
- [ ] **Static recursion/cycle check** — reject a registry-derivable call cycle (child transitively calls
      this workflow).
- [ ] `emit_mermaid`: render `NCallWorkflow` as a node (e.g. `["call child@1.0.0"]`) so `--emit-graph` / viz work.
- [ ] Tests: lowering fixtures; contract-mismatch + cycle rejection.

**3. Schema / SP / host — `db/` + `host.drift`**
- [ ] Migration `0003_workflow_call.sql`:
      - `tb_mf_operation`: `call_kind ENUM('participant','child_workflow')` (default `participant`),
        `child_workflow_id`, **child plan identity** `child_script_name` / `child_plan_version` /
        `child_content_hash`, `child_status`, the **compensation plan identity** `comp_script_name` /
        `comp_plan_version` / `comp_content_hash` (NULL when no compensation), and the child-terminal **stage**
        columns the notify SP writes (e.g. `child_return_json` + terminal status). `operation_id =
        child_workflow_id`; `operation_name = child_script_name` (no prefix → no `varchar(128)` overflow;
        **`call_kind`** is the discriminator the reverse/audit path reads — `sp_mf_operation_settle` copies it
        into the checkpoint, so `forward.operation = child_script_name`); `schema_version =
        CALL_OPERATION_SCHEMA_VERSION` (=1); **`status` / `result_json` keep their existing meaning +
        `ck_mf_operation_status_result` invariant** — none overloaded with the child plan revision.
      - `tb_mf_workflow`: `parent_workflow_id`, `parent_node_id`, `root_workflow_id`, `call_depth` (NULL for
        top-level). (The recursion key reads `script_name` here + `plan_version` / `content_hash` from
        `tb_mf_workflow_plan` — **no new workflow column**.) **No liveness-budget columns** (that's slice 2).
- [ ] `sp_mf_call_submit` — a **sibling** of `sp_mf_operation_request` (same fenced spine: plan-order →
      idempotent replay by `operation_id`+`input_hash` → insert op row (`call_kind=child_workflow`,
      `schema_version=CALL_OPERATION_SCHEMA_VERSION`, status=1) → advance continuation → append event) that **additionally, in the SAME transaction,** inserts/**reasserts**
      the child `tb_mf_workflow` (+`tb_mf_workflow_plan`) row under `child_workflow_id` with ancestry
      (`parent_*`, `root_*`, `call_depth = parent+1`) and runs the recursion guard. **Idempotent** (existing
      child → return current state). **Recursion guard:** reject if the child's plan-identity key
      `(script_name, plan_version, content_hash)` is already in the ancestor set — **reconstructed by walking
      `parent_workflow_id` links + joining `tb_mf_workflow_plan`, bounded by `call_depth`** (no denormalized
      ancestor column) — OR `call_depth > max_call_depth` → a rejection outcome (drives parent failure). Does
      **not** call `sp_mf_operation_request` (it can't compose the child-row insert + guard transactionally).
- [ ] `sp_mf_call_inspect` — read child terminal/blocked status (+ the staged child return) for the parent's
      reconcile.
- [ ] `sp_mf_child_terminal_notify` — **wake/stage-only**: on child terminal, set `child_status` + **stage**
      the child terminal outcome (e.g. `child_return_json`) in call-specific columns and mark the parent call
      operation **due** (`next_attempt = now`). **Never** sets parent `status`/`result_json` or creates a
      checkpoint — the runner (via `call_inspect`) performs the single authoritative settle/reversal; poll
      backstops a missed notify.
- [ ] `sp_mf_comp_submit` — on reversal of a completed-child checkpoint **with** a declared compensation,
      start the comp child (id `…,"comp"`) with the full **`{forward:{workflow_id,operation,operation_id,
      schema_version,input,result}}`** envelope (`forward.operation_id = child_workflow_id` correlates the
      exact child execution), the comp workflow resolved + **pinned by its exact plan identity**
      (`comp_script_name` / `comp_plan_version`
      / `comp_content_hash`) — mirroring the checkpoint's `reverse_operation_name`+`reverse_schema_version`
      pin, so a registry change can't replay a different compensation. Reuses the submit spine. **No
      reverse-child SP, no `completed→reversing`.**
- [ ] `host.drift`: typed wrappers `call_submit` / `call_inspect` / `comp_submit` + decoded outcome variants.
- [ ] Tests: SP regression — submit idempotency, inspect, terminal-notify, recursion-guard rejection,
      comp-submit.

**4. Runner — `runner.drift`**
- [ ] Dispatch `NCallWorkflow` → `call_submit` (create/reassert child); the call operation is pending.
- [ ] Await on resume → `call_inspect`: **completed** → settle call op with the child `return` (checkpoint)
      + continue; **failed** → rejection → reverse **prior** checkpoints (the failed call is **not**
      checkpointed); **blocked** → leave call op pending, parent **stays forward** (schedule inspect / await
      notify); **pending** → defer.
- [ ] Reversal of a completed-child checkpoint: declared `compensation` → `comp_submit` + await comp terminal
      (comp completed → `compensation_settled`; comp failed/blocked → reverse-block). **no-comp** → reversal
      no-op for that checkpoint.
- [ ] Recovery: adopt the durable `child_workflow_id` + settled result; idempotent reassert on re-dispatch.
- [ ] Recursion-guard rejection from submit → handled as a call failure.
- [ ] Tests: integration (extend `coordinator-singular` or a new `composition` suite).

**5. Docs / tests (acceptance)**
- [ ] Docs: `microflows_design.md` (the workflow-call model + **slice-1 boundary**); `microflows_user_guide.md`
      (`call` + `compensation` syntax); `CHANGELOG.md` (unreleased — workflow composition slice 1).
- [ ] Integration acceptance (end-to-end, root gate): **completed** (result feeds a later parent step);
      **failed** (unwind prior, failed child not re-compensated); **blocked** (parent stays forward; resolve
      child → parent proceeds; **no cascade** in A→B→C); **recovery** (crash + replay, **no live child**);
      **recursion guard** (cycle / depth → call failure → reversal); **compensating-workflow** (parent
      reversal starts a fresh comp child exactly once).
- [ ] Root `just test` green.
