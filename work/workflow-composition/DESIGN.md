# Workflow Composition — Design Plan (rev 3: preflight re-slice 1a/1b/1c + app-boundary lens; K review rounds folded in)

Concrete answers to the 7 charter questions. **Decision #1 (transport) is resolved → internal durable host
API, async + awaited.** K's round-1 corrections (**[K1]…[K6]**) and round-2 findings are folded in.

## Architecture principle — app-boundary lens (load-bearing)

Composition concentrates complexity **inside the workflow service** (coordinator / runner / SPs). That is
acceptable. What is **not** acceptable is letting it leak outward: **business apps, participants, and workflow
authors must never handle** plan hashes / `content_hash`, parent/root links or `call_depth`, leases or fencing
tokens, notify/poll or scheduling mechanics, child-id derivation, or checkpoint/reversal state. The **only**
app-facing surfaces are: (1) the author writes `call child@<plan_version> { … }` (+ optional
`compensation child@<plan_version>`, **parse-only in 1a → runtime in 1c**); (2) a compensation workflow (1c)
receives `forward.input` / `forward.result` (the rest of the envelope is runner-internal correlation it may
ignore). Every choice below is judged against this boundary — anything that would push an internal identifier
or mechanic into app surface is a defect.

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
2. **Child-owned compensation → new transition T1** [K1]: a terminal completed child is not claimable; the
   coordinator needs an internal `completed(4) → reversing(2)` transition when a parent compensates a
   completed workflow-call checkpoint. (§5)
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

**Future-slice forms — NOT in slice 1a/1b** (shown for context): `on failed` (slice 3), `compensation`
(**parse-only in 1a → runtime in 1c**, §5), `fan … key` (slice 3, §6):

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

## 5. Compensation — child-owned workflow-call MVP [K1]

> **Slice note (preflight re-slice).** Compensation runtime does not run before **slice 1c**. Slice 1b
> reverses a completed-child checkpoint as a **no-op** (no-comp). **Slice 1c MVP has one behavior:
> compensate the child call**. No compensation mode selector is exposed in MVP.

Only a *completed* child is a parent checkpoint, so compensation applies only to completed children. The
parent never absorbs the child's internal checkpoints:

- **Recursive compensation invariant.** A parent treats a child workflow as **one compensable operation**,
  not as a collection of child steps. On parent reversal, the parent compensates the **call checkpoint**. If
  the call is compensated, that compensation action asks the child workflow to compensate itself. What the
  child does with that request is the child's business: it may unwind its own completed checkpoints in reverse
  order, or no-op if it has nothing to undo. Grandchildren follow the same rule. The parent must never
  enumerate, reorder, or directly call the child's internal compensations. This preserves encapsulation:
  parent knows "undo this child call"; child knows how to respond.
- **Child compensation request — implemented by T1 for completed children.** Re-open the *terminal completed*
  child into reversal: **`completed(4) → reversing(2)`, fenced.** This is coordinator-internal machinery, not
  author-visible syntax or a parent-selected mode. Idempotency by current child state:
  - already `reversing` / `reversed` → **idempotent no-op** (the reopen already happened);
  - already `failed` → **return already-terminal, "no parent compensation to run"** [round-2 #3] — *not* a
    reopen: a failed child was never a parent checkpoint, so a parent compensation request must never act on
    it (this guards against accidental double-compensation);
  - `completed` → perform the `completed→reversing` reopen.
  Then the child handles its own compensation; child terminal compensated/no-op → parent
  `compensation_settled`; child compensation `blocked` → the parent's call operation stays pending (the
  child's block is local, §4). A child workflow that can complete under a compensable call must therefore be
  safe to receive a later compensation request **as a unit**. The exact validation rule lands with 1c, but
  the coordinator contract is clear: parent compensation triggers the child's own compensation behavior; it
  does not invent per-step child behavior.
- **Compensating-workflow is not in MVP.** A future non-MVP design may allow a fresh compensation workflow
  when "undo" is not "reverse the child", using the standard forward-context envelope. That is deliberately
  outside the 1c MVP so authors do not choose among multiple compensation modes.

> **Decision #2 (K/user) — RESOLVED for MVP.** Slice 1 runtime is **NO-COMP ONLY** — a completed-child
> checkpoint reverses as a no-op until compensation runtime lands. Slice 1c MVP implements **child-owned
> workflow-call compensation** as the single behavior: the parent asks the child call to compensate, and the
> child owns the result. The optional `compensation <wf>@<plan_version>` syntax parsed in 1a remains
> **build-rejected** for MVP; do not accept-and-ignore it, and do not expose a `compensation reverse`
> selector.

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

- **Durable child-link state** in the **`tb_mf_call` sidecar** (1:1 with the call operation, §Durable state):
  `child_workflow_id`, `child_status` (`pending|completed|failed|blocked`), `first_requested_at`,
  `last_inspected_at`.
- **Minimal inspectability (slice 1b).** Inspect surfaces, on the parent's call operation, the
  `child_workflow_id` + last-observed `child_status`, so an operator can **follow A→B→C by hand**. This is the
  *only* cross-tree affordance in slice 1 — **no blocked cascade**, no automatic ancestor rollup; the parent
  row stays `forward`. `child_status` is a **display hint** (last poll/notify observation), never the value of
  record — the child workflow row is authoritative. (Indefinite wait without a budget is safe *because* the
  tree is now inspectable.)
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
- **Notification SP** — **wake + status-hint ONLY**: child-terminal → pull the parent call operation due
  (`next_attempt = now`, **monotonic — only earlier**) + write the `child_status` **hint** in `tb_mf_call`. It
  does **NOT** stage the child's `return` as a value of record, never settles the parent
  (`status`/`result_json`), never touches parent `state`/`lease`/`continuation`, and never creates a
  checkpoint. At settle, **`call_inspect` reads the child's AUTHORITATIVE terminal result** from the child
  workflow row — the runner is the single settle authority. **Correctness never depends on notify; the
  scheduled poll is the floor** (a lost / duplicate / racy notify is always covered by poll). This removes the
  parent/child divergence class and makes a future T1 reopen (1c/2) safe by construction.

## Durable state (sketch)

**Keep `tb_mf_operation` as the generic operation spine — do NOT widen it.** A call operation reuses the op
row only for its **idempotency spine**: `operation_id = child_workflow_id` (the op-id IS the child id —
idempotent reassert falls out of the existing `(operation_id, input_hash)` replay); `input_json`/`input_hash`
= the call input; `operation_name = child_script_name` (no prefix — `child_script_name` is already
`varchar(128)`; `sp_mf_operation_settle` copies it into the checkpoint, so `forward.operation =
child_script_name`); `schema_version = CALL_OPERATION_SCHEMA_VERSION (=1)` (a fixed constant — **not** the
child plan revision; `sp_mf_call_submit`'s idempotent replay checks it like any pinned `schema_version`);
`status`/`result_json` keep the `ck_mf_operation_status_result` invariant — `requested(1)` while the child
runs, `succeeded(2)` + `result_json = child return` **only** when the runner settles a *completed* child
(reading the child's **AUTHORITATIVE** terminal result). The **only** column added to `tb_mf_operation` is the
discriminator **`call_kind` (`participant | child_workflow`, default `participant`)** — the reverse/audit path
keys on it, so a child call never mis-resolves against a same-named participant op.

**All composition-specific state goes in a sidecar `tb_mf_call`** (PK `(workflow_id, operation_seq)`, 1:1 with
the call operation, present **only** for `call_kind=child_workflow`) — so the hot, generic op table is not
widened with ~9 mostly-NULL columns and the composition complexity stays contained:
- **child plan identity** — `child_script_name` / `child_plan_version` / `child_content_hash`;
- `child_workflow_id` (also the op-id; denormalized here so the sidecar is self-contained);
- `child_status` **hint** (`pending|completed|failed|blocked`) + `first_requested_at` / `last_inspected_at` —
  display/poll bookkeeping, **not** a value of record (the child workflow row is authoritative);
- **No compensation-plan-identity columns.** 1c's single MVP mechanism (T1, reverse-child reopen) compensates
  a completed child by reopening the CHILD's OWN workflow row (`completed(4)→reversing(2)`, fenced) via the
  `child_workflow_id` already above — it never pins a SEPARATE compensation-workflow identity (that only
  exists under compensating-workflow, which is explicitly out of MVP; see §Recursive compensation invariant).
  So no `comp_script_name`/`comp_plan_version`/`comp_content_hash` are needed here, in 1b or 1c.
- **No `child_return_json` value-of-record** — notify writes the `child_status` hint only; settle re-reads
  child truth via `call_inspect`. This removes the parent/child divergence class.
- **No liveness / stuck-child budget columns** — those are slice 2.

The child workflow row (`tb_mf_workflow`) gains `parent_workflow_id`, `parent_node_id`, `root_workflow_id`,
`call_depth` (NULL for top-level). Parent states stay 1–7; T1 (1c/2) adds a `completed→reversing` edge. **No
new parent state for "waiting on child" or "waiting on blocked descendant."**

## Change surface

parser (`call`/`fan … key`/`on`/`compensation`) · ir (`NCallWorkflow`, `NFanOut`, contract + durable-list +
canonical-key validation; static cycle check; **reject `compensation` until 1c**) · host (in-process
submit/inspect + the wake/hint notification SP; comp/reverse in 1c) · runner (dispatch=submit, await=notify/
poll, settle, recursion guard; **no-comp reversal = no-op** in 1b; comp/T1 in 1c) · db (**sidecar `tb_mf_call`**
+ `call_kind` discriminator + ancestry on `tb_mf_workflow` + migration; comp/T1 SPs in 1c) · docs.

## Test plan (per slice — each slice's checklist is the authoritative list)

- **1a:** lowering/hashing parity · contract-mismatch rejection · static cycle rejection · `compensation`
  build-rejected.
- **1b:** idempotent call across retry/resume · child as ordinary forward step · **terminal replay with no
  live child** · child-**failed** = unwind prior (**failed child NOT re-compensated** [K2]) · **blocked child
  does NOT block the parent** + **no cascade** in A→B→C [K3] · **recursion guard** (cycle + depth → call
  failure, legible reason) · inspectability (operator follows A→B→C) · notify hint-only (no early settle).
- **1c:** a parent compensation request reaches a completed child **exactly once** through T1; child
  recursively owns its own compensation behavior; recovery of in-flight child compensation.
- **Slice 2:** stuck-child budget → call failure (not block); a blocked child does NOT trip it.
- **Slice 3:** fan-out stable keys + **duplicate-key rejection** [K6] · `on failed`-as-data.
- Every slice: root `just test` green.

## Decisions — resolved + slice assignments

1. **[K4] Transport — RESOLVED: internal host API** (§0.1).
2. **[K1] Compensation — RE-SLICED by preflight, MVP RESOLVED.** Slice 1 runtime is **NO-COMP only**
   (completed-child checkpoint reverses as a no-op). Slice 1c MVP implements **child-owned workflow-call
   compensation** via T1 as the single behavior: the parent asks the child call to compensate and the child
   decides whether that means unwind or no-op. 1a parses `compensation` but **validation keeps rejecting it**
   for MVP; no compensation mode selector is exposed.
3. **Liveness — split.** The **await mechanism is DECIDED for slice 1b: terminal push + poll fallback.** The
   **stuck-child timeout / budget** (default off → wait indefinitely; pause vs exclude a blocked child; the
   exhaustion rule) is **slice 2, standalone** (decoupled from compensation). **No liveness-budget columns in
   1a/1b.**
4. **[K2] Failed-as-data — RESOLVED + sliced.** "not checkpointed / unwind prior / never re-compensate" is
   **slice 1**; the `on failed` typed union is **slice 3**.
5. **Fan-out await — slice 3** (barrier-all vs policy; partial failure; comp order).
6. **Recursion — RESOLVED:** ancestor **set** of plan-identity keys `(script_name, plan_version,
   content_hash)` + `max_call_depth` (root+depth-only bounds but can't *catch* a cycle). `max_call_depth`
   default is fixed at implementation.
7. **Syntax keywords — RESOLVED** (`call` / `fan … key` / `on failed` / `compensation`; **slice 1a** parses
   `call` + `compensation` (the rest → "not in this release") and **build-rejects `compensation`** until 1c —
   parse-but-reject, never accept-and-ignore).

## Slice plan (decided — re-sliced by preflight)

> **Boundaries — explicit.** **1a** is frontend-only (no DB, no runtime). **1b** proves the **forward**
> async-call spine with **NO compensation runtime** (no-comp reversal = no-op) + minimal inspectability.
> **1c** adds child-owned workflow-call compensation as the MVP path. No inline child execution and **no
> blocked cascade**, anywhere.

- **Slice 1a — frontend only (no DB/runtime).** Grammar (`call <child>@<plan_version>` + parse-only
  `compensation`), IR `NCallWorkflow` (structural lowering/validation/hashing/mermaid). **Build REJECTS
  `compensation`** (until 1c) and a **reachable call** (op-depth gate, until 1b). Pure lowering/hashing
  parity; **zero DB**. *(Registry-backed child contract resolution + static cycle check moved to **1b.0**;
  workflow return contract is **1b.0a** — see those sub-slices. The `return <expr>` statement is recognized but
  **parse-rejected (gated) until the return_type contract lands**.)*
- **Slice 1b — forward async-call spine, NO compensation.** Sidecar `tb_mf_call` + `call_kind`; `call_submit`
  (sibling of operation_request: spine + in-txn child create + ancestry walk + recursion guard) /
  `call_inspect` (reads the child's **authoritative** terminal) / `child_terminal_notify` (wake + status-hint
  only) / settle / recovery / recursion-guard. Child-failed = rejection → unwind prior; child-blocked = parent
  stays forward, **no cascade**. **No-comp reversal = no-op.** **Minimal inspectability:** expose
  `child_workflow_id` + last `child_status` so operators follow A→B→C by hand. Stuck-child budget **OFF**
  (indefinite wait — safe because the tree is now inspectable).
- **Slice 1c — child-owned compensation path.** Ship the parent "compensate this call" behavior as the MVP:
  a completed child is reopened with the single fenced `completed→reversing` edge, then the parent awaits the
  child's own compensation result. Keep `compensation <wf>@<plan_version>` build-rejected for MVP;
  compensating workflows are a future non-MVP extension.
- **Slice 2 — stuck-child liveness budget** (§Liveness): the configurable timeout that converts a wedged
  child into a call failure (default off). Standalone, decoupled from compensation.
- **Slice 3 — fan-out + failure-as-data.** `NFanOut` (canonical keys + dedup, barrier await + comp order) and
  `on failed`-as-data / the typed failure union.

## Build checklists (dependency order)

Each stage lands with its own tests (tests are part of the slice, not a pre-phase).

### Slice 1a — frontend only (no DB, no runtime)

**Grammar — `parser.drift`**
- [ ] Parse `let <x> = call <child>@<plan_version> { <input-object> }` (also as a bare statement).
      `@<plan_version>` is a **semantic version token** `major.minor.patch` (e.g. `1.0.0`).
- [ ] Parse the optional `compensation <wf>@<plan_version>` clause (parse only — runtime is 1c). **Do not**
      add `on failed` / `fan` / `key` (slice 2/3) — leave them so they fail with a clear "not in this release".
- [ ] AST: `CallStmt { binder?, child_name, child_plan_version, input_expr, compensation? }`.
- [ ] Tests: parse fixtures (with/without `compensation`, with/without binder); malformed `call` rejected.

**IR + validation — `ir.drift`**
- [ ] New node `NCallWorkflow { node_id, child_name, child_plan_version, input_expr, result_binder?, compensation_ref? }`;
      lower `CallStmt` → it.
- [ ] Bind the child's declared **`return` type** (from the registry) to the binder; `result <x>.path`
      resolves against it (reuse the EResult typing path).
- [ ] Validate at build: child `name@<plan_version>` resolves in the registry (exact-match → pinned
      `content_hash`); **input matches child `arg`-type**, downstream uses match child `return`-type; input
      expr references only durable/in-scope values.
- [ ] **REJECT `compensation` at build** with a clear "workflow compensation not available until slice 1c"
      (parse-but-reject — never accept-and-ignore). The `compensation@<plan_version>` resolution + envelope
      acceptance check moves to 1c.
- [ ] **Static recursion/cycle check** — reject a registry-derivable call cycle (child transitively calls this
      workflow). *(Needs the registry to enumerate each pinned plan's `NCallWorkflow` edges — confirm this
      capability exists or add it.)*
- [ ] `emit_mermaid`: render `NCallWorkflow` as a node (e.g. `["call child@1.0.0"]`) so `--emit-graph` / viz work.
- [ ] Tests: lowering fixtures; contract-mismatch + cycle rejection; `compensation` rejected.

### Slice 1b — forward async-call spine (no compensation runtime)

> **1b.0a — workflows are TYPED FUNCTIONS (decided; NOT frontend-only).** A workflow has a declared
> **argument type** AND a declared **return type**, both part of **pinned plan identity / content_hash**.
> A workflow result is the **explicit value returned**, never "last op result"; op results stay separate
> internal durable facts. For 1b: returns are **object-only or unit** (`unit ⇒ {}`); a **non-unit** workflow
> must have **every successful path end in an explicit `return <expr>`**; `return <expr>` type-checks against
> the declared return type; `fail` is the unsuccessful terminal. **Contract shape (manifest-level, inline
> types; named types later):** `{ "arguments": { "type": {…} }, "returns": { "type": {…} } }` — changing
> either changes the plan hash.
> **Storage decision (override of re-derive):** the evaluated workflow return is **stored durably** as the
> workflow's terminal result (separate from per-op results). **Terminal replay reports from stored durable
> state — NOT graph re-derivation** (preserve "terminal replay needs no registry/config rebuild"). The parent
> `call_inspect` reads that **stored** child return and settles the parent call op with it (existing
> object-result path). So 1b.0a needs: `returns.type` in config + content_hash · IR validation of explicit
> `return` (object-only/unit) · **durable workflow-terminal-result storage** · terminal-replay from the stored
> return · child-call result binding from the child's declared return type. **`return` stays parse-gated until
> all of that lands.**
>
> **Atomicity (locked).** The terminal return MUST be written **atomically with completion** — never a
> second write after `operation_settle` already marked the workflow `completed` (a crash would leave
> `STATE_COMPLETED` with no return, and terminal replay would have nothing authoritative to render). The
> **final settle transition writes all three facts in ONE fenced transaction:** (a) the final op result stays
> in `tb_mf_operation.result_json`, (b) the workflow terminal return is written to the workflow-return store,
> (c) state → `completed`. Implication: `sp_mf_operation_settle` (or its final-settle path) accepts
> `workflow_return_json` when `is_final=1`; the runner's finality probe keeps the `Completed(result)` value
> and passes it into that same SP call.
>
> **Hash compatibility (locked).** `absent returns` ≡ an explicit **unit** return type. A **unit** return
> type contributes the **identity/empty suffix** to `content_hash` (so every existing workflow's hash is
> unchanged); a **non-unit** return type contributes `ir.canonical(return_type)`. Non-unit return contracts
> are thus part of plan identity while back-compat is preserved.
>
> **Unit normalization (locked).** Implicit `NReturn` is `const null` internally, but the external **unit
> result is `{}`**: a unit workflow exposes `{}` as its workflow return; **implicit unit fall-through maps to
> `{}`**; a **non-unit** workflow **rejects implicit unit fall-through** (every path must explicit-`return`);
> an explicit `return <expr>` for a non-unit workflow must be an **object matching `return_type`**. If an
> explicit unit return is ever allowed, prefer **only `return {}`** — never let `return null` become an
> app-facing result.
>
> **1b.0a split (frontend gate + runtime) — NOT frontend-only.** *(Found by sanity-check: microflows
> workflows can't return typed values yet — the implicit terminal is hard-coded `const null`, there is no
> `return` statement — so there is nothing for a parent to bind. Fixed as an explicit declared contract,
> never inferred from last-op/terminal state.)* Because a usable `return` needs durable storage (above), the
> work splits:
> - **1b.0a-frontend** (parser + IR, no DB): the `return <expr>` statement + the declared `return_type` in
>   the plan contract (an **object type** or **unit**; default unit; sourced from the manifest
>   `returns: { type: <type> }` for now — a `.mf` `returns` block can come later), folded into `content_hash`
>   per the hash-compatibility rule. **IR validation:** an explicit `return` is **object-only** and
>   type-checks against `return_type`. For a **non-unit** `return_type`, the build **rejects ANY successful
>   path that reaches the implicit unit fall-through** — *every* successful exit must produce `return_type`
>   via an explicit `return`. (This is **stricter** than "no reachable `return`": a graph that `return`s on
>   one branch and falls through on another is rejected.) Unit lets a path fall through.
> - **1b.0a-runtime** (schema + SP + runner): the durable workflow-terminal-return store + the **atomic final
>   settle** + the runner finality probe passing `Completed(result)` into it + **terminal replay from the
>   stored return**. This is the increment that **un-gates `return`**.
> - **Parent binding (lands in 1b.0):** resolve `call child@plan_version` → the child's **declared**
>   `return_type`; `result <call_id>.foo` is allowed **only if** the child return type has field `foo`;
>   **unit** ⇒ all `result <call_id>.*` paths rejected.
>
> **Sub-slice order (decided).** 1b.0a (return contract) → **1b.0 = build-time registry validation gate** (no DB, no runtime):
> resolve `call <child>@<plan_version>` against the manifest registry by **exact plan identity**; validate
> the call input against the child's declared **arg/input** contract; **bind the child `return` type** so
> `result <call_id>.path` validates downstream; **reject static call cycles at build**; keep `compensation`
> rejected until 1c. **Only after 1b.0 passes** → **1b.1 = runtime spine** (`tb_mf_call`, `call_submit`,
> `call_inspect`, `notify`, runner await/settle/recovery + the runtime recursion guard). Rationale:
> runnable calls without build-time child-contract validation would push author/config mistakes (missing
> child, wrong version, wrong input/return shape, obvious cycles) into durable execution — the exact
> complexity leak we avoid. The runtime recursion guard stays (defense-in-depth for depth/ancestry +
> registry/deploy edge cases) but does **not** replace static cycle rejection.
>
> **`max_call_depth` — default 16.** Config: `deployment.workflow_call.max_call_depth` (integer, **≥1**,
> recommended hard cap **≤64**; omitted → **16**). Exceeded → fail the call with durable reason
> `max_call_depth_exceeded`, following normal call-rejection semantics (§4 `failed`).
>
> **IR build-block inversion (1b.1 core, the inverse of 1a).** 1a made calls un-runnable two ways; 1b.1
> undoes both and makes a call a first-class durable step: (a) `advance` gains `StepOutcome::NeedCall` and
> emits it (a *settled* call → continue, like a settled op) instead of faulting; (b) `_node_depths` **counts**
> a call as `depth+1` (so `plan_length` / plan-ordering / finality include calls) instead of rejecting it.

**1b.1 — Schema / SP / host — `db/` + `host.drift`** (this checklist is kept in sync with the authoritative
SP/schema plan in `PROGRESS.md` — see that file for the full transaction-boundary/phasing rationale)
- [ ] Migration `0005_workflow_call.sql` (`0001`-`0004` already exist):
      - `tb_mf_operation`: add **only** `call_kind TINYINT` (`1=participant` default, `2=child_workflow`) +
        `CHECK(call_kind IN (1,2))`. `operation_id = child_workflow_id`, `operation_name = child_script_name`,
        `schema_version = CALL_OPERATION_SCHEMA_VERSION` (=1); `status`/`result_json` keep their existing
        meaning + `ck_mf_operation_status_result` invariant (**none overloaded**).
      - **Sidecar `tb_mf_call`** (PK `(workflow_id, operation_seq)`, FK → `tb_mf_operation`, **and a FK
        `child_workflow_id` → `tb_mf_workflow(workflow_id)`** — the child is created in the SAME transaction
        before this row, so "cannot point at a missing child" is structural, matching
        `tb_mf_workflow_plan`/`tb_mf_workflow_args`'s own FK-to-`tb_mf_workflow` pattern; rows only for
        `call_kind=child_workflow`): `child_workflow_id`, child plan identity (`child_script_name` /
        `child_plan_version` / `child_content_hash`), `child_status` **hint** + `first_requested_at` /
        `last_inspected_at`. **No compensation-plan-identity columns** — 1c's T1 mechanism reopens the child
        by its own already-known `child_workflow_id`; it never pins a separate compensation-workflow
        identity (§Durable state). **No `child_return_json` value-of-record; no liveness-budget columns.**
      - `tb_mf_workflow`: `parent_workflow_id`, `parent_node_id`, `root_workflow_id`, `call_depth` (NULL for
        top-level), all-or-none via a CHECK constraint. (Recursion key reads `script_name` here +
        `plan_version` / `content_hash` from `tb_mf_workflow_plan` — no new workflow column.)
- [ ] `sp_mf_call_submit` — sibling of `sp_mf_operation_request`. **Strict validate-then-mutate phasing**
      (no exceptions): (1) fence check; (2) existing-row check — a prior op row means this is a replay,
      compare ALL immutable fields (`operation_id`, child plan identity, `input_hash`, `call_kind`, AND the
      child's `tb_mf_workflow_args.args_canonical` byte-for-byte) → `already_submitted` on full agreement,
      `call_conflict` on any mismatch (mirrors `operation_conflict`); (3) recursion guard — ONLY reached on a
      genuinely fresh submit, read-only: reject if the child's plan-identity key `(script_name, plan_version,
      content_hash)` is already in the ancestor set — **reconstructed by walking `parent_workflow_id` links +
      joining `tb_mf_workflow_plan`, bounded by `call_depth`** (no denormalized column) — OR `call_depth >
      max_call_depth` → a structured `call_rejected` outcome (reason `call_cycle` / `max_call_depth_exceeded`),
      NOT a SIGNAL, reached with **zero writes issued so far** (this is the whole point of the phasing: the
      host commits unconditionally after reading the result document, so a rejection reached after ANY write
      would durably persist partial state — there is no rollback path, only "reject before you've written
      anything"); (4) **only now**, the single write phase: insert the op row (`call_kind=2`,
      `schema_version=CALL_OPERATION_SCHEMA_VERSION`, status=1); insert the child `tb_mf_workflow` row (its
      OWN `continuation` = its fresh starting position, NOT the parent's; `next_attempt_at` = when it becomes
      claimable) + `tb_mf_workflow_plan` row (`plan_length` = the child plan's own length) under
      `child_workflow_id` with ancestry (`parent_*`, `root_*`, `call_depth = parent+1`) — all three of
      `plan_length`/`continuation`/`next_attempt_at` are REQUIRED host-call inputs (verified against
      `sp_mf_workflow_create_planned`'s own full parameter list — without them there is nothing to write into
      `tb_mf_workflow_plan.plan_length`, no starting position for the child's first step, and no way to make
      it claimable); insert the child's `tb_mf_workflow_args` row (the call's input, canonicalized, becomes
      the child's canonical args — required for the child to be resumable via the normal planned-workflow
      path, exactly like every other planned workflow); insert the child's initial `tb_mf_workflow_event` row
      (`kind='created'`, its OWN event payload — distinct from the parent's own event payload below) —
      mirrors `sp_mf_workflow_create_planned`'s own shape, which writes workflow + plan + args + this same
      created event as one unit; insert the `tb_mf_call` sidecar row; advance the parent's continuation;
      append the parent's own event. One `rpc.commit()`
      boundary for all of it. Does **not** call `sp_mf_operation_request` or `sp_mf_workflow_create_planned`.
- [ ] `sp_mf_call_inspect` — **PURE read, zero writes** (matches `sp_mf_workflow_inspect` exactly, no
      exceptions): read the **child workflow row's AUTHORITATIVE** terminal/blocked status (the sidecar
      `child_status` is only a hint, read but never written here) for the parent's reconcile; expose
      `child_workflow_id` + `child_status` for operator inspect.
- [ ] `sp_mf_call_hint_refresh` (NEW) — small, explicitly best-effort: `UPDATE tb_mf_call SET child_status=?,
      last_inspected_at=? ...`, no fence check (a display hint, never a value-of-record), guards
      `last_inspected_at` from moving backward. The runner may call this opportunistically after any
      `call_inspect` poll that observes a non-terminal state worth refreshing the hint for (in particular
      `blocked`, since `child_terminal_notify` is terminal-only).
- [ ] `sp_mf_child_terminal_notify` — **wake + status-hint ONLY**: on child terminal, set the sidecar
      `child_status` hint + pull the parent call operation **due** (`next_attempt = now`, monotonic). **Never**
      stages the child `return` as value-of-record, never sets parent `status`/`result_json`/`state`/`lease`,
      never creates a checkpoint. The runner (via `call_inspect`) is the single settle authority; **poll is the
      floor** (a missed / duplicate / racy notify is always covered by poll). Invoked as a SEPARATE call
      strictly AFTER the child's own terminal-settle transaction has already committed — never nested inside
      it (unproven cross-workflow lock-ordering risk otherwise: every existing publication fences + commits
      exactly one workflow's rows per transaction, and this would be two).
- [ ] `sp_mf_checkpoint_reverse_noop` (NEW) — the no-comp reversal mechanism a call checkpoint actually needs:
      fenced, verifies reverse-order + `reversal_state=1` (idempotent `already_reversed` on retry) + the
      checkpoint's operation is `call_kind=child_workflow` (defensive `not_call_checkpoint` guard), then the
      SAME `reversal_state` 1→2 transition `reverse_settle` performs — but WITHOUT ever requiring/touching a
      `reverse_invocation_id` (nothing is ever dispatched for a call checkpoint, so the existing
      `reverse_request`→dispatch→`reverse_settle` flow can structurally never reach it). Requires extending
      `sp_mf_checkpoint_reverse_head`'s `Pending` outcome (+ the host `ReverseHeadOutcome::Pending` variant)
      to also carry `call_kind`, so the runner can branch to this SP BEFORE ever calling `_compensation_for`.
- [ ] `host.drift`: typed wrappers `call_submit` / `call_inspect` / `call_hint_refresh` /
      `child_terminal_notify` / `checkpoint_reverse_noop` + decoded outcome variants; extend
      `ReverseHeadOutcome::Pending` with `call_kind`.
- [ ] Tests: SP regression — submit idempotency (incl. args-row agreement), submit rejection leaves zero
      partial rows (recursion/depth), inspect (authoritative read, pure), hint-refresh (best-effort),
      terminal-notify (hint only, no settle), checkpoint-reverse-noop (call-checkpoint no-op reversal,
      defensive `not_call_checkpoint` guard on a participant checkpoint).

**Runner — `runner.drift`**
- [ ] Dispatch `NCallWorkflow` → `call_submit` (create/reassert child); the call operation is pending.
- [ ] Await on resume → `call_inspect`: **completed** → settle call op with the child's **authoritative**
      `return` (checkpoint) + continue; **failed** → rejection → reverse **prior** checkpoints (the failed call
      is **not** checkpointed); **blocked** → leave call op pending, parent **stays forward** (schedule inspect
      / await notify), **no cascade**; **pending** → defer (poll).
- [ ] Reversal: on `reverse_head`'s `Pending`, check the (now-extended) `call_kind` field BEFORE calling
      `_compensation_for` — if `child_workflow`, call `checkpoint_reverse_noop` directly (skip the
      compensation-binding lookup entirely, since a call checkpoint never has one) and loop back to
      `reverse_head` to continue descending; if `participant` (existing default), the existing
      `_compensation_for` → dispatch → `reverse_settle` flow is unchanged. (The compensation runtime proper
      is 1c; a declared `compensation` cannot reach the runner because 1a rejects it — this is the no-op path
      for the ordinary "child call has nothing to compensate" case, not 1c's compensation mechanism.)
- [ ] Recovery: adopt the durable `child_workflow_id` + settled result; idempotent reassert on re-dispatch;
      terminal replay needs **no live child**.
- [ ] Recursion-guard rejection from submit → handled as a call failure with its legible reason.
- [ ] Tests: integration (extend `coordinator-singular` or a new `composition` suite).

**Docs / acceptance (1b)**
- [ ] Docs: `microflows_design.md` (the workflow-call model + **1a/1b/1c boundaries** + the app-boundary
      lens); `microflows_user_guide.md` (`call` syntax; `compensation` "coming in 1c"); `CHANGELOG.md`.
- [ ] Integration acceptance (root gate): **completed** (result feeds a later parent step); **failed** (unwind
      prior, failed child not re-compensated); **blocked** (parent stays forward; resolve child → parent
      proceeds; **no cascade** in A→B→C); **recovery** (crash + replay, **no live child**); **recursion guard**
      (cycle / depth → call failure → reversal); **inspectability** (operator reads `child_workflow_id` +
      `child_status` and follows A→B→C).
- [ ] Root `just test` green.

### Slice 1c — child-owned compensation path

- [ ] Keep the 1a build-rejection of `compensation <wf>@<plan_version>` for MVP; no compensation mode selector
      or compensating-workflow runtime is exposed.
- [ ] Implement `sp_mf_T1` child-compensation reopen (`completed(4)→reversing(2)`, fenced; `failed` →
      already-terminal-no-comp) plus the child-compensation **await + settle + recovery** path.
- [ ] Preserve the recursive compensation invariant: the parent compensates one call checkpoint by asking the
      child to compensate itself; the parent never reaches into child checkpoints directly.
- [ ] Acceptance: parent reversal sends one compensation request to a completed child exactly once; the child
      owns unwind/no-op behavior without parent introspection; failed child never re-compensated; recovery of
      an in-flight child compensation.
