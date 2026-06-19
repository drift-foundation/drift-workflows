# manual-ir-frontend  (roadmap §7: parser + type checker — built **IR-first, parser last**)

## Short-term objective
Define the **typed workflow IR** the runner executes, prove its **semantic
validation/type-checking** and its **control-flow execution** (conditionals + replayable
pure loops) against **manually constructed IR**, with pinned **replay regressions across
every control-flow construct** — and only THEN add the textual DSL parser, lowering into
the already-proven IR. Prove control-flow machinery and durable restart before mechanical
syntax work.

## Current behavior / problem
**(Largely resolved — the manual-IR runtime surface is now complete; see "Current status".)**

Originally the runner executed only a **flat, ordered manual plan**: `struct PlanStep
{ operation, input_json }` (config `"plan"`), resolved step-by-step, with **no conditionals,
no loops, and no typed values**. That has been built out: `microflows/runner/src/ir.drift`
now holds a **typed control-flow graph IR** the runner executes — straight-line, `if`, `case`,
finite array expressions (`map`/`filter`/`fold`), cross-branch result merge, `let`, `return` —
with structural validation, typed expression validation (operation input/result contracts), and
graph-authoritative `content_hash`. The flat plan is lifted to a degenerate straight-line graph,
so legacy plans still run.

The design's value model (microflows_design.md §4: JSON-compatible values, schemas authoritative
at remote + durable boundaries, typed path traversal, variables / arrays / objects / optionals /
`if` / `case` / early return, deterministic local iteration + collection transforms) is now
representable and executable over **manually authored** IR/config. The ONE remaining gap is the
textual front end: per §7 the parser/type checker is scheduled **after** the proven runtime, and
§8.3a keeps the manual IR/config as the loader until the parser lowers source into it.

## Accepted design decisions
- **IR-first, parser-last (the agreed sequence).** Steps 1–4 build and prove the IR,
  validator, executor, and restart semantics on **hand-constructed** IR; step 5 adds the
  parser, which lowers into the SAME proven IR and reuses the same validator. No `.mf`
  source, lexer, grammar, or diagnostics before step 5.
- **V1 value model — deliberately CLOSED and minimal** (a subset of the §4 target; enough to
  prove typed branches, pure transforms, remote-input construction, replay, and result
  consumption — not the eventual language):
  - **Types:** `Null`, `Bool`, `Int`, `Float`, `String`, `Array<T>`, **closed-field** `Object`,
    `Optional<T>`.
  - **Expressions:** literals; local bindings (`let`); references to the **durable expression
    sources** (see "Determinism is structural"); typed field/index traversal; object/array
    construction; boolean operators; comparisons; explicit basic arithmetic + string
    operations (intrinsics — with pinned deterministic behavior, below).
  - **Collection expressions:** `map`, `filter`, `fold` over **finite arrays** only. (No
    standalone `for-each` — it adds nothing once side effects are prohibited.)
  - **Operation-result references use stable IR node IDs** — never operation names or
    execution-sequence guesses. A result is named by the producing `Operation` node's id.
  - **Raw JSON is an explicit boundary escape hatch only.** An unvalidated raw value may NOT
    be used directly as a branch condition or a loop source — it must be validated into a
    typed value first (so control flow is always driven by typed, deterministic data).
  - **Operation input/output contracts expose these types to the validator** — but this is
    NOT a general schema language; the contract is just the typed shape in this value model.
  - **Explicitly excluded in V1:** implicit coercion, unions, user-defined types, generics,
    arbitrary functions, and `while`.
- **Determinism is STRUCTURAL — the IR exposes no non-deterministic source.** There is no
  clock, random, environment, filesystem, network, live-config, or callback expression in the
  IR at all (not "allowed but checked" — simply absent). A pure expression may read ONLY:
  (a) pinned IR constants, (b) the durable workflow arguments, (c) settled operation results
  referenced by stable node id, and (d) locals derived from those. Replay is sound by
  construction; the earlier "determinism guard" reduces to "these are the only sources." (Raw
  JSON entering at a boundary must still be validated into a typed value before it can drive a
  branch or loop.)
- **Durable workflow arguments (added in Step 1).** Branches and transforms need instance-
  specific input, so each workflow instance receives exactly **one JSON object**:
  - The script declares its **closed-field argument TYPE** (in the V1 value model). This
    **declared type IS part of the script `content_hash`** — changing the argument contract
    changes the pinned revision identity (like any IR contract). Only the **per-instance
    argument VALUES are excluded** from `content_hash`.
  - The instance object is **validated against the declared type, then canonicalized to
    ordered-key compact canonical JSON (the same form `_input_hash` uses), BEFORE creation**.
  - It is persisted **as the canonical UTF-8 document (bytes)** in a small immutable child
    record created **ATOMICALLY with the workflow and the plan pin** (one aggregate commit —
    a single-`workflow_id` immutable fact, justified per `storage_portability.md`).
  - **Resume always reads the durable record, never submission/CLI input** (mirrors how
    operation requests are recovered).
  - Reusing a `workflow_id` with different argument content returns **`workflow_conflict`**,
    decided by a **byte-for-byte comparison of the canonical UTF-8 document** — NOT ordinary
    SQL text equality (which is collation-sensitive), and NOT merely a caller-supplied hash.
    A stored hash may *accompany* the canonical bytes (for indexing / a fast pre-check) but
    cannot **replace** the content comparison. (Distinct from `plan_conflict` = a different
    plan identity.)
  - The argument VALUES are **instance data**, **separate from** the continuation, the audit
    events, and `security_context_ref`.
- **Intrinsics define their own deterministic behavior.** The basic numeric, indexing,
  missing-field, and error semantics of the V1 operations are **specified by the IR**, not
  inherited from the host language/platform — same inputs yield the same result and the same
  error on every executor (so replay and cross-host execution agree).
- **Two hard control-flow invariants (from the design, non-negotiable):**
  - **No control-flow cycle may directly or indirectly invoke a remote operation** — loops
    are **side-effect-free** (pure local computation / collection transforms). Remote
    operations live only on the straight-line / branch spine, never inside a cycle. (This
    is necessary but **does not** by itself bound termination — see the V1 loop shape.)
  - **Every remote operation is an implicit durable suspension boundary** (no `await`). The
    remote ops are the **only** durable boundaries; the continuation advances only there.
- **Persist ONLY at remote-operation boundaries; replay pure control flow.** Branch
  decisions, loop iterations, and `let` bindings are **not** durable points and write
  **no** continuation. After a crash the runner re-executes the deterministic pure control
  flow forward from the **last durable remote-op boundary** (the last settled operation, or
  the workflow start) until it reaches the next remote op, then reconciles that op against
  durable state. This requires pure control flow to be **deterministic from durable inputs
  only** — the initial arguments plus settled operation results; nothing non-deterministic
  (clock, random, live config beyond the pinned IR) may steer a branch or loop.
  Consequence: a pre-settle restart recomputes the identical next-op input (same
  `input_hash` → idempotent); a post-settle restart skips the op (durable result). This is
  the storage-portability model unchanged — continuation is a head field that marks the
  remote-op boundary, and the pure path between boundaries is re-derived, never stored.
- **V1 loop shape: structurally finite transforms over finite arrays** — `map` / `filter` /
  `fold` over an already-materialized finite array, **not** an arbitrary `while`-style cycle.
  Termination is **structural** (the array is finite), not inferred. Unbounded /
  condition-driven cycles are out of scope for V1.
- **Pinned identity extends to the control-flow graph.** `content_hash` must cover branch
  targets, loop bodies, and value/schema bindings — not just the flat op list — so a
  changed control-flow graph is a different revision (never substituted), consistent with
  the ScriptRegistry exact-match model.
- **IR home: a dedicated runner-owned module, `microflows/runner/src/ir.drift`** (decided).
  The typed value model + node/graph types + validator live there — NOT embedded further in
  `runner.drift`, and NOT in the public microflows host/storage package. The manual loader
  and runner consume it now; the parser targets the same IR later. Extract into a separate
  package only when a second artifact genuinely needs it.
- **The flat plan becomes a degenerate straight-line graph**, so all existing dispatch /
  settle / checkpoint / reversal / recovery / ScriptRegistry suites stay green — the graph
  IR subsumes today's plan rather than replacing its proven machinery.

## Concrete implementation plan (the five steps, ordered)

**1 — Typed workflow IR + durable arguments** (new module `microflows/runner/src/ir.drift`).
Define the IR the runner consumes:
- **Value model:** the CLOSED V1 set above — `Null`/`Bool`/`Int`/`Float`/`String`/`Array<T>`/
  closed-field `Object`/`Optional<T>`, typed field/index traversal, the listed expressions
  (intrinsics with pinned deterministic numeric/indexing/missing-field/error behavior), and
  `map`/`filter`/`fold` over finite arrays. Operation contracts expose these types (no
  separate schema language). Determinism is structural — the only expression sources are
  pinned constants, durable arguments, settled results (by node id), and derived locals. Raw
  JSON only at the boundary, validated before use.
- **Control-flow graph:** nodes each with a **stable IR node id** — `Operation` (remote;
  durable-suspension boundary + optional compensation binding; its result is referenced by
  node id), `If` / `Case` (branch), `Loop` (finite `map`/`filter`/`fold`), `Let`/`Bind`
  (pure local compute), `Return` / terminal — with branch/next edges.
- **Durable workflow arguments:** the script declares a **closed-field argument type** (part
  of the IR, so it is in `content_hash`); each instance's one JSON object is validated against
  that type + **canonicalized to ordered-key compact canonical JSON before creation** and
  persisted **as canonical UTF-8 bytes** in a small **immutable child record committed
  atomically with the workflow + plan pin** (extend the `create_planned` command). A new
  aggregate read returns the durable args; resume reads it (never CLI/submission input). Reuse
  with different content → `workflow_conflict`, decided by a **byte-for-byte canonical
  comparison** (an accompanying hash may pre-check but never replaces it). Argument VALUES are
  excluded from `content_hash`.
- Re-express `PlanStep`'s flat list as a degenerate straight-line graph; turn
  `_run_forward` into a graph interpreter (existing behavior preserved). Extend
  `content_hash` over the full graph (nodes, ids, targets, contracts, **the declared argument
  type**, bindings) — but NOT the per-instance argument values. The manual loader and
  `runner.drift` consume `ir.drift`; the future parser targets the same types.

**2 — Semantic validation / type checking (on manual IR).** A graph-aware validator at
registry build (evolves `_validate_plan`):
- **operation + compensation resolution** — every `Operation` resolves to a participant +
  pinned schema_version; every compensation binding resolves.
- **input/result contract compatibility** — each op input conforms to its typed input
  contract; consumers of a result conform to its typed result contract (typed field/index
  traversal checked). Contracts are expressed in the V1 value model — not a schema language.
- **stable result references** — every operation-result reference names an existing
  `Operation` **node id** (never an operation name or sequence guess); a result is referenced
  only after that node on the path.
- **argument-type well-formedness + references** — the script's declared argument type is a
  closed-field object type; every reference into the durable arguments traverses only declared
  fields with compatible types (same as result references). (The per-instance object is
  validated against this type at submission, before creation — see Step 1.)
- **branch target validity** — every `If`/`Case` edge targets a real node; no dangling or
  duplicate targets; `case` exhaustive or has a default.
- **typed, validated control-flow drivers** — a branch condition and a loop source must be
  typed values (the listed types); an **unvalidated raw JSON value may NOT drive a branch or
  a loop** (it must be validated into a typed value first).
- **side-effect-free, structurally-finite loop constraints** — no `Operation` (remote) node
  reachable within any loop body (the hard rule), AND every loop is a finite collection
  transform, not a `while`-cycle (reject unbounded/condition-driven cycles). Termination is
  structural — guaranteed by the finite collection, not inferred.
- **reachable terminal states** — every path reaches a terminal (complete / early-return /
  reversed); no dead ends. With remote ops only on an acyclic spine and loops finite by
  construction, every execution terminates.

**3 — Execution: conditionals + replayable pure loops.** Extend the interpreter to evaluate
`If`/`Case` over typed values and take the branch, and to run finite pure loops locally.
These pure boundaries write **no** continuation: the continuation still advances **only** at
remote ops (request-before-dispatch / settle), exactly as today. On restart the interpreter
re-executes the deterministic pure control flow forward from the last durable remote-op
boundary (or the start) to re-derive the same branch/loop results and the next op's input,
then reconciles that op against durable state. Preserve request-before-dispatch / settle /
checkpoint / reversal at every remote op on the spine.

**4 — Pinned compile/run replay regressions across every control-flow construct.** The
durable boundaries are the remote ops (pure boundaries are not restart positions); the
regressions prove that **replaying the pure control flow across each construct is
deterministic**. SP + integration tests that crash/restart between
remote ops with, in between: a branch (restart re-derives the same branch and the next op's
input); a finite pure loop feeding a remote op (restart recomputes the identical
transformed input); a chain that crosses a branch mid-spine; early return; and **reversal
across a taken branch** (the compensation stack built along the chosen path unwinds
correctly). Each asserts deterministic resume — same branch/loop result, recomputed-
identical input (same `input_hash`), settled ops skipped, zero duplicate remote effect, and
**no continuation write at any pure boundary**.

**5 — Textual DSL parser (LAST).** Add the `.mf` lexer / grammar / type binding that
**lowers into the step-1 IR** and reuses the step-2 validator unchanged; add diagnostics.
The mechanical syntax layer, deliberately last. (Scope reuse of `../drift-lang`'s
parser/type-checker/IR/diagnostics when we get here.)

## Files likely affected
- **`microflows/runner/src/ir.drift` (NEW, decided)** — the typed value model + control-flow
  node/graph types + the graph validator. Runner-owned; not in the public host/storage
  package. The manual loader, the runner, and (later) the parser all consume it.
- `microflows/runner/src/runner.drift` — consume `ir.drift`: build the IR (subsume
  `PlanStep` / `_build_plan`), interpret it (`_run_forward` → graph executor),
  `content_hash` over the graph; validate + canonicalize the argument object at submission,
  read the durable args on resume. The continuation positions are essentially unchanged
  (still mark remote-op boundaries); pure control flow is re-derived, not encoded.
- `microflows/db/*` — **ONE new durable child for arguments** (e.g. `tb_mf_workflow_args`):
  a small immutable per-`workflow_id` record holding the **canonical args document as
  `VARBINARY` (ordered-key compact canonical UTF-8 bytes)**, written ATOMICALLY with the
  workflow + plan pin. This evolves `sp_mf_workflow_create_planned` (args param + a
  **byte-for-byte canonical comparison** — `VARBINARY` byte equality, never collated
  `VARCHAR` text — → `workflow_conflict`; an optional accompanying hash may pre-check but not
  replace it) and adds a read proc (`sp_mf_args_get`) + host variants — a single-aggregate
  immutable fact justified per `storage_portability.md` (which already flags collation/text
  semantics as non-portable mechanism). NO other durable change: **pure control-flow
  boundaries still persist nothing**, and remote-op boundaries reuse the existing
  operation/checkpoint/event + continuation model. Treat "a *control-flow construct* needs a
  new durable record" as a red flag (args are instance INPUT,
  not control flow).
- Integration config + fixtures — richer manual-IR graphs (branch/loop) + restart seeds at
  each construct; `integration/coordinator-singular/test.py` replay regressions;
  `db/tests/sp_operation_test.py` only if a durable shape changes.
- Eventually `microflows/doc/microflows_design.md` (IR / value-model / control-flow
  sections as proven) and `../drift-lang` (step 5).

## Verification criteria
- Full root `just test` green at every step; all existing flat-plan + reversal +
  ScriptRegistry suites stay green (graph IR subsumes the flat plan).
- Step 2: the validator **rejects each malformed-IR class** with a distinct domain error
  (unresolved op/compensation, contract-incompatible input/result, bad node-id result ref,
  dangling branch target, raw-JSON control-flow driver, remote-op-in-cycle / non-finite loop,
  unreachable/dead terminal, ill-formed argument type) and accepts valid graphs.
- Step 1/args: an argument object is **validated against the declared type + canonicalized
  (ordered-key compact UTF-8) before creation** and persisted as those bytes atomically with
  the workflow + plan pin; **resume reads the durable args** (not CLI/submission); reusing a
  `workflow_id` with **different canonical bytes → `workflow_conflict`** (decided byte-for-byte,
  not by SQL text collation; same content incl. reordered keys → idempotent). The **declared
  argument TYPE is in `content_hash`** (changing the contract is a new revision); only the
  per-instance VALUES are excluded (a value change is a `workflow_conflict`, not a new
  revision). A test reorders keys + changes a byte to prove canonical-byte equality vs conflict.
- Step 3: conditionals + pure loops execute end-to-end (branch chosen by an argument/result
  value; loop transforms an array; result feeds a remote op).
- Step 4: a crash/restart between remote ops resumes deterministically by **replaying the
  pure control flow** — same branch/loop result, recomputed-identical input (same
  `input_hash` → idempotent), settled ops skipped, zero duplicate remote effects, **no
  continuation write at any pure boundary**; reversal across the taken path unwinds the
  correct checkpoint stack.
- The ONLY new durable state is the per-instance arguments child (a single-`workflow_id`
  immutable fact, justified per `microflows/doc/storage_portability.md`). **Pure control flow
  adds no durable table and no continuation position**; remote-op boundaries reuse the existing
  model.

## Current status and next action
**Manual-IR runtime surface COMPLETE (steps 1–4) AND the parser (step 5) COMPLETE — the full V1
lowering surface is landed.** The typed IR, durable arguments, structural + typed validation,
control-flow EXECUTION (straight-line, `if`, `case`, finite array expressions, cross-branch merge,
`let`, `return`), graph-authoritative `content_hash`, and pinned replay/reversal regressions are all
landed; and the textual front end now covers the WHOLE V1 IR — straight-line + `if`/`case` +
`let`/arg/local refs + operation naming & `result` refs + `merge` + `map`/`filter`/`fold` — a `.mf`
source lowers into the EXACT config the manual IR executes, with NO new runtime semantics. Full
`just test` green (certified driftc 0.33.42 / ABI 17; integration **128/128**, was 101). See
Progress.md for the per-chunk record.

Parser (`microflows/runner/src/parser.drift` + `--lower-source` CLI): `args` → `argument_type`,
`op … { input/result }` → operation contracts, `steps { … }` → a flat `"plan"` (const-only
straight-line) or a control-flow `"graph"`. `if`/`case` lower to graph nodes with pre-order ids that
re-converge at the join (selection from durable args); `let <name> = <expr>` lowers to `NLet`, and
operation inputs are expressions (`{…}` const / `const <json>` / `arg <path>` / `local <name>[.path]`
/ `result <name>[.path]`); `let <bind> = <op> <input>` NAMES an operation so `result <bind>` lowers to
`EResult` (the alias lives only in the parser → result refs are stable under source formatting and
alias-rename); and `if … else … merge <name> = <v1> | <v2>` lowers to `NMerge` (selecting a
branch-local op result at the join — result aliases are globally unique so the arms name distinct
branch results); and `let ys = map/filter/fold <source> [from <init>] each <elem> <body>` lowers to
`NLoop` (the body is expression-only, so no remote op can appear inside iteration). Reuses
`_build_plan`/`parse_graph`/`validate_graph`/`type_check_graph`/op-depth/`content_hash` unchanged (no
new IR nodes, execution paths, or durable state). Source is the SOLE authority on contracts (base
contracts stripped), and `--lower-source` runs the real build/validation path (DB-free) before
printing, so an invalid source/config (unknown op, op-imbalanced branch, missing `case` default,
undefined `local`, undominated/cross-branch `result` ref, non-predecessor merge source, non-array loop
source, `elem`/`as` collision, …) fails AT lowering. Parity proven: parser-lowered and hand-authored
configs produce the IDENTICAL `--emit-content-hash` and execute the same; pure boundaries
(`let`/`merge`/`map`/`filter`/`fold`) write no event, resume recomputes derived/merged/loop values
from durable state, and reversal compensates only the taken branch + shared downstream op.

Next action: **diagnostics/spans** (upgrade parse errors from byte offsets to line/column + context).
Remaining niceties: `case`-join merge, possible `../drift-lang` reuse. The core V1 lowering is DONE.

## Open questions / blockers
- **✅ RESOLVED on Drift 0.33.35 — recursive-IR clean form landed.** `core.Box<T>` + the
  typed-catch SIGSEGV fix shipped certified (ABI 17, `cbf32feb`). `ir.drift` migrated to
  `core.Box<IrType>` recursion (`core.box`/`.get()`), `_one`/`_assert_well_formed` dropped, and
  a real TYPED `catch IrError(e)` reconstructing `IrError(message = e.message)` (all-scalar
  schema → projection supported). Repro re-run confirms old `move e` is now a compile error,
  not a SIGSEGV. See Progress.md.
- **✅ RESOLVED — typed-catch non-scalar limitation handled via errors-as-values.** The 0.33.35
  rebuild rejected pre-existing config code (`build_gateway`/`build_host` re-raised a caught
  `ConfigError`/`HostConfigError` whose `kind` is a non-scalar variant field). The toolchain
  team confirmed this is an intentional v1 limitation, not a defect, and blessed
  **errors-as-values** as the long-term design. Applied: config parsers now return
  `Result<_, …ConfigError>` (movable native error, no throw/catch round-trip), and `pool.open`'s
  Result is matched directly. Full `just test` is GREEN on certified **0.33.36** (and 0.33.35).
  See Progress.md and the toolchain response in `/tmp/drift-announce/2026-06-15T12-45-56Z-response-*.md`.
- **How far back does replay start?** Re-deriving the pure path from the last settled op
  vs from the workflow start — confirm the interpreter can cheaply replay to the next
  remote-op boundary and that this is the simplest correct cursor (no per-construct
  continuation). Decide in step 1/3.
- **`../drift-lang` reuse** (parser / type checker / IR / diagnostics) for step 5 — scope
  TBD when we reach it.

*(Resolved: IR home → `microflows/runner/src/ir.drift`; the closed V1 value model; **determinism
is structural** (IR exposes no clock/random/env/fs/net/live-config/callback; sources = pinned
constants, durable args, settled results by node id, derived locals); **durable arguments**
(one validated+canonicalized JSON object — ordered-key compact canonical UTF-8 bytes — as an
immutable child, resume-reads-durable, `workflow_conflict` by byte-for-byte canonical compare;
the declared argument TYPE is in `content_hash`, only instance VALUES are excluded);
**intrinsics** pin deterministic numeric/indexing/missing-field/error behavior; pure
boundaries persist nothing; loops are finite array transforms — see Accepted design
decisions.)*

## Relevant review findings
Per-finding detail lives in Progress.md (the review ledger for this lane). Landed themes from the
multiple review rounds, all fixed + regression-tested:
- **Strict config parsing** — `parse_graph`/expr/case/merge sub-objects reject unknown/extra/missing
  keys and duplicate arms by exact key NAMES, not just counts (no silent drops).
- **Branch durability + reversibility** — transition-faithful reversing fixtures (matching audit
  head + args child); reversal compensates only the taken branch/case path + shared downstream
  checkpoints; restart unwinds from the checkpoint stack, not graph control flow.
- **Finite array expressions** — loop element name must not collide with ANY graph binder
  (path-insensitive shadowing rejection); a non-array loop source is rejected at type-check
  (direct literal AND one laundered through a merge), not deferred to a replay fault.
- **Typed expression validation** — closed object literals infer field-by-field; a const reaching a
  typed op input through a `let`/`merge`/`loop` binder is value-validated (`validate`) or rejected
  (the three-way `Known`/`Unknown`/`Imprecise` inference — `Imprecise` is never permissive);
  assignability (`null`/`T` ⊑ `Optional<T>`); compensation input must match the forward checkpoint
  payload, and compensation type tags are folded into `content_hash`.

Carries forward the storage-portability constraints: single-`workflow_id` aggregate; continuation as
the restart authority; `content_hash` exact-match identity over the control-flow graph (incl. arg +
operation/compensation type contracts); durable suspension only at remote operations (never inside a
pure node — `if`/`case`/loop/merge/`let` write no continuation/event).
