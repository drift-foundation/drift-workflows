# Microflows User Guide

**Audience:** teams building durable business jobs on Microflows — workflow
authors and participant-service implementers (e.g. PushCoin Bookkeeper:
payments, charges, settlement).
**Scope:** how to design, test, and run a workflow against the V1 runtime. For
the internal design rationale see `microflows_design.md` (the as-built runtime
and language are §12); for the participant wire contract see
`singular/doc/singular-protocol.md`.

> **Read this first — the mental model.** Microflows is a *coordinator*, not a
> compute engine. A workflow **orchestrates typed remote calls** to participant
> services that own the data, the money, and the business logic. The workflow
> itself does **no computation**: it calls an operation, branches on the result,
> carries values to the next call, and — when a later step fails — runs the
> declared compensations in reverse. **Thin orchestrator, smart participants.**
> If you find yourself wanting to add two numbers or compare values in the
> workflow, that work belongs in a participant. See [Limits](#7-capability-envelope--limits)
> before you design anything — they are load-bearing.

---

## 1. The two artifacts you write

A runnable workflow is **a `.mf` source file + a deployment config**:

1. **`workflow.mf`** — the workflow: its argument shape, the operation contracts
   it calls, and the ordered steps/control flow. This is the business logic
   *shape*. It contains **no endpoints and no secrets**.
2. **`deploy.json`** — the trusted deployment config: which participant service
   each operation routes to, the pinned `schema_version`, and each operation's
   compensation. Endpoints, auth, and routing live **here**, never in the `.mf`.

You combine them once, at deploy time, with the runner's `--lower-source`
frontend, which produces a single **runnable config**:

```bash
microflows-runner --config deploy.json --lower-source workflow.mf > runnable.json
```

`--lower-source` parses the `.mf`, type-checks it, validates the merged graph,
and prints the runnable config — or **fails here**, before anything durable
happens, with a precise diagnostic (see [§8](#8-diagnostics--troubleshooting)).
The printed `runnable.json` is what the service actually runs.

---

## 2. Quick start

A one-call workflow: reserve an order, complete.

`reserve.mf`:

```mf
# Arguments: the durable instance input, validated + frozen at submission.
args {
  request: { reservation: string }
}

# The remote operation this workflow calls. Both contracts are optional but
# recommended — they are type-checked before any dispatch.
op reserve {
  input:  { reservation: string }
  result: { reserved: string }
}

steps {
  reserve arg request
}
```

`deploy.json` (routing — see [§6](#6-the-deployment-config)):

```json
{
  "worker_id": "runner-1",
  "db": { "...": "microflows MariaDB connection" },
  "participants": [
    { "id": "orders",
      "transport": { "kind": "http", "endpoints": ["http://orders.svc:8080"],
                     "selection": "ordered_failover" },
      "auth_profile": null }
  ],
  "operations": [
    { "name": "reserve", "participant": "orders", "schema_version": 1 }
  ]
}
```

Lower, then submit an instance:

```bash
# 1. Produce the runnable config.
microflows-runner --config deploy.json --lower-source reserve.mf > runnable.json

# 2. Submit a new instance (a 32-hex workflow id you choose; --arguments = SUBMIT).
microflows-runner --config runnable.json \
  --workflow-id 00000000000000000000000000000001 \
  --arguments '{"request":{"reservation":"order-42"}}'
# -> prints a status line, e.g. {"workflow":"completed"}

# 3. RESUME the same instance later (omit --arguments -> drive from durable state).
microflows-runner --config runnable.json \
  --workflow-id 00000000000000000000000000000001
```

`--arguments` present = **submission** (creates/reasserts the instance; changing
the args for the same id is a `workflow_conflict`). `--arguments` absent =
**resume** (the runner drives the instance forward from durable state; it reads
the *durable* arguments, never the command line).

---

## 3. The `.mf` language

A file has three top-level blocks: `args`, zero or more `op`, and `steps`.
`#` starts a comment to end-of-line.

### 3.1 Types

```text
int  float  bool  string  null      scalars
{ field: T, … }                     closed object (exact fields)
[ T ]                               array of T
T?                                  optional (postfix)
```

Used in `args` (the instance argument type) and `op` input/result contracts.

### 3.2 `args` — the instance argument type

```mf
args {
  order:    { id: string, amount: { value: int, currency: string } }
  customer: { id: string }
}
```

Submitted `--arguments` JSON is validated against this type and frozen as the
durable, canonical instance input. The argument **type** is part of the
revision identity; the per-instance **values** are not (a value change is a
conflict, not a new revision).

### 3.3 `op` — operation contracts

```mf
op authorize {
  input:  { customer: string, amount: { value: int, currency: string } }
  result: { auth_id: string }
}
```

`input`/`result` are optional. When present they are type-checked across the
whole graph before dispatch (a const or projected value that doesn't fit the
declared input is rejected at lowering). Declaring them is strongly recommended
for business jobs.

### 3.4 Expressions — how a value reaches an operation

An operation input (and a `let` value, a loop source, etc.) is **exactly one**
expression:

```text
{ …json… }            a fully CONSTANT object        -> { "const": {…} }
const <json-value>    any constant (scalar/array/obj) -> { "const": … }
arg   <path>          a durable-argument subtree      -> { "arg": [..] }
local <name>[.path]   a let/loop/merge binding        -> { "local": {…} }
result <name>[.path]  a named operation's result      -> { "result": {…} }
```

`<path>` is `ident(.ident)*` — object-field projection (e.g. `arg order.amount`,
`result auth.auth_id`).

> **Build inputs from dynamic parts.** An object/array literal may carry
> **expression-valued** fields/elements, so you assemble an operation's input
> inline: `reserve { customer: arg c.id, amount: arg order.amount }` or
> `pack [ arg a, result b, const 3 ]`. Construction is a **pure** value step
> (no operations inside; recomputed on replay), type-checked against the
> operation's input contract before dispatch. A **fully-constant** literal is
> exactly the constant object/array it looks like. So you do **not** need to
> pre-shape the arguments document to match each op — wire values directly.

### 3.5 Statements

```mf
reserve arg request                 # operation step = a remote call
let total = sum arg lines           # NAMED op: `total` aliases its result
confirm result total                # ... feed that result downstream

let p = arg request.reservation     # pure value binding (no remote call)
reserve local p

if flag { … } else { … }            # branch on a Bool durable argument
case tier { "gold" { … } "silver" { … } default { … } }   # N-way branch

let ids = map arg items each it local it.id      # finite PURE transform
let kept = filter arg items each it local it.ok
let last = fold arg items from const null each it local it
```

- **Operation step** `op <expr>`: dispatches a remote call. `let n = op <expr>`
  *names* the call so `result n` references its result later.
- **`let n = <value-expr>`**: a pure binding (no remote call). Recomputed on
  replay; never a durable boundary.
- **`if` / `case`**: branch on a durable-argument path (the decision comes from
  durable args). `case` requires a `default`, last.
- **`merge`** (a join): `if … else … merge picked = result a | result b` (and
  the `case` form with one value per arm + default) selects a branch-local
  result at the join to feed shared downstream work. See
  [§4.2](#42-branches-that-rejoin-merge).
- **`map`/`filter`/`fold`**: finite transforms over an array. **The body is a
  pure expression — you cannot call a remote operation inside a loop.**

---

## 4. Control flow

### 4.1 Branch

```mf
args { request: { reservation: string }, express: bool }
op reserve  { input: { reservation: string } result: { reserved: string } }
op expedite { input: { reserved: string } }
steps {
  let r = reserve arg request
  if express {
    expedite result r
  } else {
    # nothing -> flows straight to the join
  }
}
```

### 4.2 Branches that rejoin (`merge`)

When each branch produces a value and shared downstream work needs "whichever
one ran", use `merge`:

```mf
steps {
  if vip {
    let a = reserve { "reservation": "vip-pool" }
  } else {
    let b = reserve { "reservation": "std-pool" }
  }
  merge picked = result a | result b
  confirm local picked          # confirm gets the taken branch's reservation
}
```

The `case` form lists one value per arm **plus the default, last**:
`merge picked = result a | result b | result d`.

### 4.3 Finite transforms

```mf
# Project a field from each element (pure; no remote calls in the body).
let ids = map arg orders each o local o.id
```

`map` builds an array of body results; `filter` keeps elements whose (Bool) body
is true; `fold` threads an accumulator from `from <init>`. Use these to *select*
or *reshape* an array you then pass to one participant operation — not to fan out
N remote calls.

---

## 5. Worked example: a payment-capture saga

Authorize, then capture; on capture failure, **void the authorization**
automatically. Each operation's input is built inline from the arguments via
expression construction ([§3.4](#34-expressions--how-a-value-reaches-an-operation)),
so the arguments document doesn't need to be pre-shaped per op.

`capture.mf`:

```mf
args {
  customer: { id: string }
  order: { amount: { value: int, currency: string } }
}

op authorize {
  input:  { customer: string, amount: { value: int, currency: string } }
  result: { auth_id: string }
}
op capture {
  input:  { auth_id: string }
  result: { capture_id: string }
}
op record_ledger {
  input:  { capture_id: string }
}

steps {
  # The authorize input is BUILT inline from two argument subtrees (expression construction).
  let auth     = authorize { customer: arg customer.id, amount: arg order.amount }
  let captured = capture { auth_id: result auth.auth_id }
  record_ledger { capture_id: result captured.capture_id }
}
```

`deploy.json` operations — note the **compensations**:

```json
"operations": [
  { "name": "authorize", "participant": "gateway", "schema_version": 1,
    "compensation": { "operation": "void_authorization", "schema_version": 1 } },
  { "name": "void_authorization", "participant": "gateway", "schema_version": 1 },
  { "name": "capture",   "participant": "gateway", "schema_version": 1,
    "compensation": { "operation": "refund_capture", "schema_version": 1 } },
  { "name": "refund_capture", "participant": "gateway", "schema_version": 1 },
  { "name": "record_ledger", "participant": "ledger", "schema_version": 1 }
]
```

Behavior you get for free:
- **Durable & effectively-once.** Each call persists a request *before* dispatch;
  a crash/lost-ack recovers by re-polling the stable operation id — the
  participant replays, the effect happens once.
- **Compensation.** If `record_ledger` fails terminally, Microflows runs the
  compensations for the completed checkpoints in reverse: `refund_capture` then
  `void_authorization`. (Reversal is itself a remote dispatch; participants must
  implement the reverse operations.)
- **Resume.** Kill the runner mid-flight; re-invoke with the same `--workflow-id`
  and no `--arguments`. It re-derives the pure path and reconciles the in-flight
  operation without re-dispatching settled ones.

> Every non-final operation **must** declare a compensation (else lowering fails
> with a build error) — so a partial saga can always unwind.

---

## 6. The deployment config

```json
{
  "worker_id": "runner-1",
  "db": { "...": "the microflows MariaDB connection" },

  "participants": [
    { "id": "gateway",
      "transport": { "kind": "http",
                     "endpoints": ["http://gw-a:8080", "http://gw-b:8080"],
                     "selection": "ordered_failover" },
      "auth_profile": null }
  ],

  "operations": [
    { "name": "authorize", "participant": "gateway", "schema_version": 1,
      "compensation": { "operation": "void_authorization", "schema_version": 1 } }
  ]
}
```

- **`participants`** — the lasting trust boundary: id → transport/endpoints +
  selection. `auth_profile` **must be `null`/absent in V1** (any value is
  rejected — see [Limits](#7-capability-envelope--limits)).
- **`operations`** — op name → participant + pinned `schema_version` +
  optional `compensation`. Operation name, URL, schema, and (future) auth come
  from this **trusted config**, never from workflow input.

The `.mf` declares *which* operations a workflow calls; this config binds those
names to real services. Keeping them separate means the same workflow can target
staging vs production by swapping the config.

### 6.1 Revisions & pinning

The runnable config has a stable **`content_hash`** over the graph + bindings +
argument type + contracts. Print it with:

```bash
microflows-runner --config runnable.json --emit-content-hash
```

A running instance is pinned to its `content_hash`; a resume requires an exact
match, else `revision_unavailable` — the runtime **never** silently substitutes
a different revision. Formatting, key order, and declaration order don't change
the hash; a genuine semantic change does. Deploy by giving the service the exact
revision(s) your in-flight instances need.

---

## 7. Capability envelope & limits

**Supported and proven (integration 142/142, real HTTP + Singular-backed participant):**

- Durable saga: call → settle → **checkpoint** → **compensate on failure** →
  resume after crash → effectively-once execution.
- Branch/route (`if`/`case`), pass data (`arg`/`result`/`local` projections),
  named results, cross-branch `merge`, finite pure `map`/`filter`/`fold`,
  optional typed contracts checked before dispatch.

**Not in V1 — design around these (they are deliberate, not bugs):**

| You cannot… | Do this instead |
|---|---|
| **Compute in the workflow** (arithmetic, string building, `if x ≥ y`) — `IrExpr` is projections + constants only | Compute in a **participant**; the workflow passes its result on |
| **Call a remote op inside a loop** (no dynamic fan-out) | Model the batch as **one** participant operation, or unroll to a fixed shape |
| **Loop unboundedly** (`while`) | Use finite `map`/`filter`/`fold` over a known array |
| **Authenticate participant calls** (`auth_profile` must be null) | Trusted-network participants only for now; **do not** put a real external gateway on the open wire until auth lands |
| **Branch on a computed predicate** | Have a participant return a Bool/tag; branch on `if`/`case` of that |

**Production-readiness note for money movement.** The orchestration core is
**proven for internal/trusted-network workflows** (durability, recovery, and
compensation are exercised end-to-end), but it is **not yet production-grade for
open-wire money movement**: **(1) participant authentication** is not implemented
(`auth_profile` must be null), and **(2)** the thin-orchestrator model means
*all* computation and side effects live in participants — accept that as a design
rule before committing. For internal/trusted-network jobs you can design, test,
and run today; reserve "production-grade for payments" until those blockers
close.

---

## 8. Diagnostics & troubleshooting

`--lower-source` fails fast with a **structured diagnostic** — a stable `code`,
the source position (`line`/`column`/`byte_offset`, 1-based), and a caret:

```
parse error [unknown-keyword]: unexpected top-level keyword at line 1, column 1 (byte 0)
  frob { ... }
  ^
```

The same event is emitted to `std.log` with machine-parseable fields (`code`,
`byte_offset`, `line`, `column`, `expected`, `found`) for tooling. Common codes:
`unknown-keyword`, `unknown-type`, `expected-expression`, `expected-token`,
`unterminated-block`, `case-arm-after-default`, `case-merge-arity`,
`expected-each`, `unknown-result-name`.

Semantic errors surface at lowering too (because `--lower-source` runs the real
build path): an unknown operation, an op-imbalanced branch, a missing `case`
default, an undefined/undominated `local` or cross-branch `result`, a
non-predecessor merge source, a non-array loop source, an `elem`/`as` collision,
or a type-contract mismatch. Fix them before the config is ever submitted.

**Run-time status** is a JSON line on stdout: `{"workflow":"completed"}`,
`{"workflow":"pending"}` (in flight / deferred — resume later),
`{"workflow":"reversed"}` (fully compensated), or an error status such as
`invalid_config` / `revision_unavailable`. A `pending` instance is resumed by
re-invoking with the same `--workflow-id` and no `--arguments`.

---

## 9. Implementing a participant

A participant is **any** service that satisfies the wire contract in
`singular/doc/singular-protocol.md` — Singular-backed is preferred inside
PushCoin, but not required at the wire level. The contract in one breath:

```text
PUT  /microflows/v1/operations/{operation}/{operation_id}
  idempotent creation at a caller-chosen id (input + canonical input hash +
  schema version). Same id + same input -> same logical operation/result;
  same id + DIFFERENT input -> 409. 202 = accepted/in-progress.

GET  /microflows/v1/operations/{operation}/{operation_id}
  durable lookup: succeeded|failed (terminal), pending, deferred (busy, with a
  due time), indeterminate. 404 = no record (Microflows may safely re-PUT).
```

Your participant owns the **business effect, its idempotency, and its durable
outcome replay**; Microflows owns coordination. Each forward operation that can
be compensated needs a **reverse operation** (same protocol, its own stable id)
that accepts the forward op's input as *its* input — declare it as the
`compensation` in the deployment config. Build a participant against the
conformance reference (`microflows/participant-stub/`) and its black-box harness
before integrating.

---

## 10. Command reference

```text
microflows-runner --config <base.json> --lower-source <wf.mf>
    Lower a .mf to a runnable config on stdout (DB-free). Fails on any parse/
    type/validation error. No dispatch.

microflows-runner --config <runnable.json> --emit-content-hash
    Print the revision's content_hash (hex). DB-free.

microflows-runner --config <runnable.json> --workflow-id <32-hex> --arguments <json>
    SUBMIT a new instance (validate args, freeze, drive forward). Re-asserting
    with different args for the same id -> workflow_conflict.

microflows-runner --config <runnable.json> --workflow-id <32-hex>
    RESUME (omit --arguments): drive the instance from durable state.
```

(`--operation`/`--input` exist for a legacy single-operation submission and are
not used for `.mf`-authored workflows.)

---

*See `microflows_design.md` §12 for the as-built runtime and language internals,
and `singular/doc/singular-protocol.md` for the full participant contract.*
