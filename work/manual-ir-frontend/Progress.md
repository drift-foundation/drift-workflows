# manual-ir-frontend — Progress / status

Charter (objective, decisions, plan, verification): [README.md](./README.md).

## Status: **Step 1 in progress — arg substrate + typed argument contract LANDED**

The lane is scoped (IR-first, parser-last). Step 1's durable argument substrate (1a) AND the
typed argument contract (the value-model slice of 1b: `ir.drift` + validation +
type-in-content_hash) are landed and green (full `just test`, ABI 17). Step 1b(ii) chunk 1 —
control-flow graph IR **types + canonical hashing + flat-plan compatibility** — is landed
(below). Execution (interpreter) + structural validation + making the graph authoritative for
content_hash are the remaining 1b(ii) chunk.

## Landed: control-flow graph IR — chunk 1 (Step 1b(ii): types + canonical + flat compat)
- **Graph node types in `ir.drift`** (additive; runner behavior unchanged): `IrExpr` (closed leaf
  source set — `EConst`/`EArg`/`EResult`/`ELocal`, each path-projectable; determinism is
  structural), `LoopKind` (`LMap`/`LFilter`/`LFold` — finite array transforms, no `while`),
  `IrCaseArm`, `IrNode` (`NOperation`/`NLet`/`NIf`/`NCase`/`NLoop`/`NReturn`), `IrGraph`. Control
  flow is a FLAT node table keyed by stable id with node-id edges (not nested nodes) → finite
  value, no `Box` needed; a loop body is a pure `IrExpr` (structurally forbids a remote op in a
  loop). Only `NOperation` is a durable suspension point.
- **`graph_canonical(graph)`** — deterministic encoding over entry, every node (kind + id +
  fields + edges), branch targets, loop bodies, and per-op name+input. Node order normalized
  sorted-by-id and `NCase` arms sorted-by-`match_const`, so declaration order never affects
  identity. Length-prefixed (collision-free). Pure/config-only; the runner will later compose it
  with resolved op + compensation bindings + arg type when the graph becomes authoritative.
- **`flat_to_graph(steps)`** — re-expresses the flat operation plan as a degenerate straight-line
  graph (`n0→n1→…→nN` `NOperation`s + terminal `NReturn` unit). Behavior-preserving. Each step's
  input is **canonicalized** (key-ordered compact) into the `EConst`, so key order/whitespace in
  config can't create a false revision change once the graph is authoritative; returns
  `Result<IrGraph, IrError>` (malformed input constant → `Err`).
- **`canonical_json(text)`** — the IR home's "canonical JSON literal" definition (strict parse +
  lexicographic key-ordered compact encode, same scheme as the runner's `_canonical_json`, which
  may later delegate here). `EConst`'s field is named `canonical_json` and its contract (producer
  passes a `canonical_json`-normalized string; `graph_canonical` hashes verbatim) is explicit;
  `IrCaseArm.match_const` shares it.
- **DEFERRED to the next chunk (per decision):** wiring the runner's live `_content_hash` to the
  graph (and recomputing fixtures), the interpreter, and structural validation. This chunk does
  NOT touch `_content_hash`/`_plan_canonical`/fixtures — revision identity + runtime unchanged.
- **Tests:** `microflows/runner/tests/unit/ir_graph_test.drift` (new, DB-free, compiled
  standalone with `ir.drift`; base+asan) — builder shape, canonical determinism, node- and
  arm-order independence, field-change distinctness, INPUT CANONICALIZATION (reordered keys hash
  identically; malformed → `Err`), empty plan. Gated by the **Microflows component gate**: a
  `test` recipe in `microflows/runner/justfile`, invoked from `microflows/justfile` `test`
  (`cd runner && just test`, DB-free, before the DB lock), so `just test-microflows` runs it.
  Full gate green on certified 0.33.36.
- Drift notes (0.33.36): cross-module visibility needs an explicit `export {}` AND `pub` on each
  STRUCT field (variant ARM fields are visible without `pub`); a `match` arm must bind ALL of an
  arm's fields by exact name (unused tolerated); a binding named `json` collides with the
  `std.json` alias (the const field is `canonical_json`); can't take `&` of a call temporary
  (bind first); expression-form `val x = match {…}` can't `return` from an arm — use
  `var x = <default>; match {…}` (statement form).

## Landed: manual-IR control flow, slice 1 — `if` (graph interpreter reachable for one branch shape)
The graph interpreter is now REACHABLE from config for a non-degenerate shape (a single `if`),
with NO parser/DSL and NO new storage. Full gate green; integration 77/77.
- **Manual-IR `graph` config** (`ir.parse_graph`): a directly-authored control-flow graph (config
  `"graph"` key) — `operation` / `if` / `let` / `return` nodes + `const`/`arg`/`result`/`local`
  expressions. Intentionally small (no loops, no `case`, no parser). `const` values are
  canonicalized into the `EConst` literal. **STRICT parsing** (graph is content-hashed identity):
  each node kind has EXACTLY its allowed keys and an expression is EXACTLY ONE variant — unknown/
  extra keys or multiple variant keys are rejected (no silent drop / priority). `_registry_build`
  uses `"graph"` when present, else lifts the flat `"plan"` to a degenerate graph;
  `_is_planned_config` recognizes EITHER as planned (so `--arguments` submission works for graphs).
- **Execution beyond the degenerate guard:** `_assert_degenerate` → **`_assert_executable`** —
  rejects only `NLoop` (loops still deferred) and derives `plan_length` from **`ir.op_depth`**.
  The forward loop drives `ir.advance` for `operation`/`if`/`let`/`return`.
- **Operation sequencing for branches** (guardrail 1 — validated at BUILD, not handled at runtime):
  durable seq = EXECUTION POSITION (`settled.len+1`); replay maps each settled result back to the
  node id `advance` chose. `ir.op_depth` requires a UNIFORM per-path operation count (rejecting
  op-unbalanced branches at registry build → invalid_config / revision_unavailable, before any
  dispatch), so the path's final op lands at `seq == plan_length` and the storage layer's finality
  derivation (`is_final ⟺ seq==plan_length`) holds UNCHANGED — no new durable state.
- **Reversibility (graph-level compensation):** `_assert_reversible` (via `ir.nonfinal_operations`)
  rejects at BUILD any graph where a NON-FINAL operation (one that can execute before the final
  position, so a later op could fail and begin reversal) lacks a compensation binding — generalizes
  `_validate_plan`'s flat-plan rule across branches, so reversal can never strand at
  `no_compensation_binding`.
- **Result references** (guardrail 2): `EResult(node)` is valid only if that NOperation DOMINATES
  the reference (enforced by `validate_graph`). Branch tests keep each branch op's result
  branch-local / rely on terminal replay of the final durable op result; no cross-branch merge
  (an explicit merge model is deliberately deferred).
- **Tests:** unit — `ir_exec_test` (op_depth straight-line=2 / balanced-if=1 / **unbalanced→Err**;
  `nonfinal_operations`; `parse_graph` strictness — unknown node key / multi-variant expr / unknown
  variant / extra top-level key all rejected). Integration C6 (graph-driven): if-true dispatches
  only branch A (B zero calls), if-false only B, no durable record at the branch boundary (pure
  NIf — same event count as a 1-op plan), terminal replay returns the final durable op result, a
  **claimable RESUME** (nudge `next_attempt_at`) re-derives the branch through `ir.advance` from the
  DURABLE args — staying `pending`; had it used `{}`/CLI it would `graph_replay_fault` — with the
  false-branch contrast under one config, an op-unbalanced if rejected before dispatch, and a
  NON-COMPENSABLE non-final op rejected before dispatch. Existing straight-line planned suite green.
- **Deferred (still):** finite loops (`NLoop` validated, not executed); `case`; cross-branch result
  merge; the source-language parser.
- **Strictness follow-up (review finding):** `_parse_expr` validated the `result`/`local` sub-objects
  by COUNT only (`_reject_extra(_, 2)`), so `{"result":{"node":"n1","bogus":1}}` (2 keys, no `path`)
  silently dropped `bogus`. Now `_reject_unknown_opt_path` validates the actual key NAMES (only
  `<required>` + optional `path`); unit cases (`ir_exec_test`) reject `{node,bogus}` / `{name,bogus}`.

## Landed: manual-IR control flow, slice 2 — branch durability + reversibility

Proves a branch graph (`if`) is durable and reversible across the real failure paths, using the
existing graph config surface only. No loops, no parser/DSL, no cross-branch result merge (the
merge node's input is a `const`), no new durable state (no new tables/columns; one reversing seed
in existing fixture tables).
- **Mid-flight branch resume reconciles GET-first (no second PUT):** submit `flag:true` with the
  branch op PENDING (Singular left Working) → op requested-not-settled. A claimable RESUME RECOVERS
  the durable request and reconciles by GET (participant still Working → 202), never re-PUTting the
  same operation id, and never re-evaluating into the other branch. Test
  `graph_branch_midflight_resume_get_first_no_second_put` asserts `put` delta 0, `request` delta ≥1,
  op row unchanged.
- **Forward failure after a taken branch:** a 2-deep branch graph (`reserve brA/brB` → shared final
  `reserve fin` → return; uniform `op_depth`=2). With `flag:true` and the FINAL op rejected (400):
  the taken branch's compensable op settles+checkpoints, then the rejection BEGINS reversal and
  compensates ONLY the taken path (`release brA`) → reversed. Test
  `graph_branch_forward_fail_reverses_taken_path_only`: exec+2, checkpoint reversed, trigger op
  present, and the UNTAKEN branch (`brB`) has zero operation rows, zero checkpoints, zero execution.
- **Restart across branch reversal:** seeded `WF_REVERSE_BRANCH` (state=reversing, direction=reverse,
  one active checkpoint = taken branch op A, payload `brA`; no pin in the seed). A fresh resume
  driven with the branch graph config (matching plan pin inserted live from the runner's own
  `--emit-content-hash`) reaches `_run_reversal`, which unwinds from the CHECKPOINT STACK
  (`reverse_head`) and reads compensation from the operations registry (`_compensation_for`) — it
  NEVER consults the graph. Test `graph_branch_reversal_restart_unwinds_from_checkpoint`: reversed,
  reverse direction, compensation EXACTLY once (`exec`+1, `release` on durable payload `brA`); a
  graph re-evaluation would have dispatched a forward reserve, so `exec`=1 proves checkpoint-driven.
- **Coverage:** the manual graph `if` now has forward, resume, terminal-replay, AND reversal
  coverage. Integration 81/81 (was 78); full `just test` green.

## Landed: chunk 2 PART 2 — runner adopts the graph
Delivered in verifiable stages.
- **Stage 1 (LANDED, full gate green):** the graph is authoritative for IDENTITY + VALIDATION.
  `_registry_build` now builds the degenerate graph (`_build_graph` via `ir.flat_to_graph`) and
  runs `ir.validate_graph` — an invalid graph throws `RunnerError`, surfaced by the existing
  build catches as `invalid_config` (submission, pre-claim) or `revision_unavailable` (resume,
  post-claim). `content_hash` switched from `_plan_canonical` to GRAPH identity:
  `ir.graph_canonical(graph)` ‖ `_graph_bindings(cfg, graph)` (per-NOperation resolved
  schema_version + participant + compensation) ‖ `ir.canonical(arg_type)`. `ScriptRevision`
  carries the graph. Seed fixtures recomputed via a new DB-free `--emit-content-hash` runner
  mode (actual algorithm, no Python reimplementation): `[e1,e2]` `019dc1f4…`→`01fba1fa…`,
  frplan `01ebb9ce…`→`0161c3ad…`. Execution is STILL plan-based here, so behavior is unchanged;
  full `just test` green on 0.33.38.
- **Stage 2 (LANDED, full gate green):** the graph is authoritative for EXECUTION. `_run_forward`
  is now advance-driven: gather durable args (`args_get`) + settled results (`OpResult`, op at
  seq K = node `n{K-1}`), then loop `ir.advance` → `NeedOperation` (recover-or-derive request,
  resolve, request, dispatch, settle; append the settled result and continue) / `Completed`
  (report completion per existing final-op result rules — last settled op's result, not the
  graph's unit return) / `Fault` (defensive defer). `seq = settled.len + 1` with a guard that the
  node id equals the degenerate `n{seq-1}` (non-degenerate execution is out of scope). The flat
  forward loop + `ScriptRevision.plan` are gone. Behavior preserved: full `just test` green
  (singular 16/16, microflows 20/20 + SP 110/110, integration 67/67) — existing straight-line,
  mid-plan-resume (wf20), and reversal-restart (wf21) tests all pass through the advance path.
  The dispatched input is now the canonical (key-ordered) form; `input_hash` already canonicalized
  so operation identity/idempotency is unchanged (key order is semantically irrelevant).
- **Stage 3 (LANDED, full gate green):** explicit behavioral regressions for the graph-driven
  runner path (no parser/control-flow expansion). Integration 71/71:
  - `graph_straight_line_parity` — fresh 2-op planned workflow driven entirely by `advance`:
    completed, result = final op's, exec == 2, distinct per-seq op ids, checkpoint payloads = each
    op's input (parity with the former flat path).
  - `forward_plan_resume` (strengthened) — mid-plan resume via `advance`: op1 settled → only op2
    dispatched (exec +1, op1 not re-dispatched), op2 checkpoint carries its canonical input,
    completion result = op2's `{reserved:e2}`.
  - terminal replay already covered by `terminal_rerun_multiop_final_result` +
    `terminal_replay_registry_config_independent` (returns the FINAL op result `{reserved:c2}` —
    not op1's, not a unit value — from durable state, even with a malformed registry).
  - `content_hash_input_key_order_insensitive` + `content_hash_changes_on_semantic_graph_change`
    — via the runner's own `--emit-content-hash` (not a reimplementation): reordered input keys →
    identical hash; a changed input value → different hash.
  - No non-degenerate-graph integration case (config builds only degenerate graphs; no
    parser/control-flow surface to author one, and we didn't invent one). That path is covered by
    `ir_exec_test` validation + the runner's build-time `_assert_degenerate` (rejects before
    claim) and the post-claim node-id guard (defers + releases lease).

  **Stage 3 completes the graph-authoritative STRAIGHT-LINE runner proof: the graph is now the
  single source for validation, identity (content_hash), and forward execution. Parser / non-
  degenerate control-flow EXECUTION (branches, finite loops, Let) remain separate follow-up work
  (the IR types + validation + interpreter for them already exist in `ir.drift`, exercised by
  `ir_exec_test`, but are not yet reachable from config and not yet executed by the runner).**

## Landed: graph validation + interpreter — chunk 2 PART 1 (ir.drift; runner not yet switched)
Authoritative validation + a pure-control-flow interpreter, both in `ir.drift`, fully unit-tested.
The runner does NOT yet build/validate/execute via the graph and `content_hash` is UNCHANGED —
that adoption (+ fixture recompute + restart integration tests) is chunk 2 PART 2.
- **`validate_graph(g) -> Optional<String>`** — rejects: empty graph, duplicate ids, missing
  `entry`, dangling edge targets, control-flow CYCLES (DFS; loops are finite transforms, not
  back-edges), UNREACHABLE nodes (dead code must not affect revision identity — `graph_canonical`
  hashes every node — nor escape validation), non-terminal sinks (valid terminal = `NReturn`),
  `EResult` to a non-`NOperation` / self / non-dominating op, graph-level `ELocal` with no
  dominating `NLet`, duplicate `NLet` local names (V1: globally unique — replay resolves a local
  by its single binding, so same-named shadowing would be order-sensitive), duplicate `NCase`
  `match_const` (replay takes the first match while canonical sorts arms → ambiguous), and bad
  finite-loop shape (map/filter carry no accumulator). Dominance computed simply (X dominates Y
  iff Y reachable but unreachable once X removed) — fine for tiny graphs.
- **`advance(g, arguments, settled) -> StepOutcome`** — replays pure control flow from durable
  args + settled op results to the next durable boundary: `NeedOperation(node,op,canonical input)`
  for the next UNSETTLED op, or `Completed(result)` at `NReturn` (`Fault` is defensive). Evaluates
  `EConst`/`EArg`/`EResult`/`ELocal` (path projection) and `NLet`/`NIf`/`NCase`; SETTLED ops are
  skipped (their result feeds later `EResult`). Restart-deterministic: same (args, settled) →
  same step. Persistence unchanged — only `NOperation` is a durable boundary. **Loop EXECUTION is
  a follow-up** (`NLoop` is validated but `advance` faults on it); no config produces loops yet.
- **Tests:** `microflows/runner/tests/unit/ir_exec_test.drift` (new) — validation accept/reject
  per defect class; replay over a degenerate graph and a branching graph (EArg/EResult/ELocal
  projection, NLet, NIf true/false, NCase, operation-skip, restart determinism). base+asan, gated
  under `test-microflows` (runner `test` recipe now runs both `ir_graph_test` + `ir_exec_test`).
- Drift note (0.33.36): `Array.pop()` did NOT shrink `.len` as expected in a stack worklist
  (infinite loop / hang) — use a head-index worklist (`while h < work.len { … h+=1 }`) instead.

## Landed: typed argument contract (Step 1b — value-model slice)
- **`microflows/runner/src/ir.drift`** (new, runner-owned): the closed V1 value-type model
  `IrType` (Null/Bool/Int/Float/String/Array/closed Object/Optional — recursive arms via
  `core.Box<IrType>` indirection for a finite layout) + `parse_type`/`parse_object_type` (config
  encoding), recursive `validate(value, type)`, and a deterministic `canonical(type)` encoding
  (fields normalized to sorted-by-name).
- **Each script revision declares a closed-object argument type** (config `argument_type`,
  default the empty object). The runner **validates submitted `--arguments` against it before
  `create_planned`** (malformed → `invalid_arguments`, no durable state).
- **Type-in-content_hash, NON-conditional:** `_content_hash` appends `ir.canonical(arg_type)`
  always (empty object encodes as `"O{}"`). Existing planned seeds recomputed
  (`[e1,e2]`→`019dc1f4…`, frplan→`01ebb9ce…`); Python repro validated against the prior hashes.
- **Tests:** integration `args_valid_full` / `_optional_absent` / `_reordered` /
  `args_missing_field` / `_extra_field` / `_wrong_scalar` / `_nested_object_mismatch` /
  `_array_element_mismatch`, plus the kept `forward_args_*` (now with a declared `{a,b:int}`
  type). Green via full root `just test` (counts live in the harness output, not here).
- Drift note: directly-recursive variant arms (`TArray(IrType)`) are rejected
  (`E_RECURSIVE_VALUE_TYPE`); recursive arms use `core.Box<IrType>` (move-only unique-ownership
  heap indirection — `core.box`/`.get()`). Toolchain is **0.33.36 / ABI 17** (`Box<T>` + the
  typed-catch fixes landed in 0.33.35/0.33.36; the 0.33.32 reload substrate remains the future
  mechanism for §10 ScriptRegistry SIGUSR1 reload).

## Also landed (review findings, four rounds)
Round 4:
- **Narrow catches (Medium):** the two submission/post-claim config-build blocks now
  `catch RunnerError(e)` (the expected validation failure → `invalid_config` /
  `revision_unavailable`) instead of catch-all, so an UNEXPECTED defect propagates and fails
  loudly. `ir.parse_object_type` returns a `Result<IrType, IrError>` so the runner needs no
  error-type-qualified catch. (The internal IrError→Result conversion now uses a TYPED
  `catch IrError(e)` reconstructing `IrError(message = e.message)` — `IrError` is all-scalar, so
  binder projection is supported; the earlier SIGSEGV/catch-all workaround was removed once
  0.33.35/0.33.36 fixed the typed-catch path.)

Round 3:
- **Strict JSON (Medium):** config loading, runner argument validation, op-input
  canonicalization, and host argument canonicalization use `json.parse_strict` (reject
  duplicate keys + non-standard numbers — so a duplicate `type` key can't bypass the
  unknown-key guarantee and canonical identity is well-defined).
- **Live host args_get coverage (Medium):** new e2e `live_args_test.drift` exercises
  `create_planned` (Created/Exists/WorkflowConflict), `args_get` (Found/NotFound, canonical
  round-trip), and `plan_get` (Found/NotFound) against a real DB (registered in the curated
  LIVE_TESTS list) — an SP-name/decode defect in HostImpl would now be caught.
- **Durable-absence assertions + clean config errors (Low):** a malformed submission config
  now reports `invalid_config` (not a process-fatal crash); tests query the workflow/args
  tables to pin that a rejected submission (bad args, unknown type key) writes NO durable row.

Earlier rounds:
- **Resume plan_get/claim race (High):** after an argument-less resume whose initial pin read
  found none, the runner RELOADS the authoritative pin post-claim (never resolves/reports on an
  empty pin / `plan_length=0`), AND — when the reload finds no pin (a concurrently-created
  LEGACY id we claimed) — **releases the lease** before reporting not_found (no leak). Pinned by
  a seeded legacy workflow wf27 + `forward_legacy_id_no_lease_leak`.
- **Existing reassertion (Medium):** arg-type validation runs only on a FRESH submission; an
  existing pin reasserts via create_planned's byte-compare against the DURABLE args, so valid
  v1 args aren't rejected against a rolled-out v2 type. Test `forward_args_existing_not_revalidated`.
- **Exact numerics (Medium):** Int = integer-shaped + in-range (overflow rejected); Float =
  float-shaped + finite (integer-shaped and non-finite rejected) — no coercion. Tests
  `args_int_rejects_float_shaped`/`_overflow`, `args_float_rejects_integer_shaped`/`_non_finite`.
- **Reject unknown declaration keys (Low):** `parse_type`/`_parse_field` reject any key beyond
  the allowed set (a typo can't silently drop data / desync content_hash). Tests
  `args_type_unknown_key_rejected`, `args_type_declaration_order_hash_equivalent`.
- **Compiler-team guidance:** recorded the confirmed driftc bug (direct recursive value types →
  `E_RECURSIVE_VALUE_TYPE` coming; use 1-element-`Array` owned indirection) and added
  `_assert_well_formed` to enforce the exactly-one-element invariant.
- **Seeded args fixtures (Medium):** every planned fixture (wf20–26) now has a
  `tb_mf_workflow_args` row (`{}`), so fixtures match production creation.

## Landed earlier: durable argument substrate (Step 1a)
- **Schema:** `tb_mf_workflow_args` — immutable per-`workflow_id` child holding the canonical
  args as `mediumblob` (ordered-key compact UTF-8 bytes), FK to `tb_mf_workflow`.
- **Atomic create:** `sp_mf_workflow_create_planned` takes `arg_args`, writes the args row in
  the SAME transaction as workflow + plan + event; exists-path compares stored bytes
  **byte-for-byte** (`<=>` on blob, no collation) → new `workflow_conflict` (same plan name,
  different args). Validates args is a JSON object.
- **Read:** `sp_mf_args_get` returns the durable canonical args (found/not_found).
- **Host:** `create_planned(args_json: &String)` + `WorkflowConflict` variant; new `args_get` +
  `ArgsGetOutcome`; exported. **The host CANONICALIZES the args at the command boundary**
  (`_canonical_args` → ordered-key compact UTF-8) and sends BYTES, so reordered keys can never
  produce a false conflict (canonical form enforced, not trusted).
- **Runner:** SUBMISSION vs RESUME is signalled by **`--arguments`**: supplied → a planned
  submission that CREATES/REASSERTS (so a reused id with changed args → `workflow_conflict`),
  omitted → a resume (drive from durable state, never re-create/re-check). The submission
  arguments come from the CLI, NOT deployment config. `_run_planned` creates on submission
  (not pin-absent), so existing workflows no longer bypass argument-identity checking.
- **Tests:** SP create/get/idempotent/`workflow_conflict`/not-object + **concurrent
  same-ID/different-args race** (`create_planned_concurrent_diff_args_conflict`); integration
  `forward_args_submit_completes` / `forward_args_reorder_equivalent` (reordered keys are
  idempotent) / `forward_args_resubmit_conflict`. All create_planned call sites + cleanup
  loops + 6 fresh planned submissions (`--arguments {}`) updated. Green via `just test`.
- Review round addressed: (1) existing-workflow argument identity (submission reasserts),
  (2) args from submission CLI not registry config, (3) canonical form enforced in the host.
- Pending (the only carry-over): resume-reads-`args_get` wiring — lands when the IR actually
  consumes arguments (Step 3). (Arg-TYPE-in-content_hash is DONE — see the typed-argument-
  contract section above.)

## Decided up front
- **IR home:** new runner-owned module `microflows/runner/src/ir.drift` (not in `runner.drift`,
  not in the public host/storage package).
- **Persist only at remote-op boundaries.** Branches, loops, and `let`s write no
  continuation; restart **replays** deterministic pure control flow from the last durable
  remote-op boundary. Requires pure control flow to depend only on durable inputs.
- **V1 loops are structurally finite array transforms** (`map`/`filter`/`fold` over a finite
  array — no `for-each`, no `while`); termination is structural.
- **Closed, minimal V1 value model:** `Null`/`Bool`/`Int`/`Float`/`String`/`Array<T>`/
  closed-field `Object`/`Optional<T>`; literals, `let`, durable input/result refs, typed
  field/index traversal, object/array construction, boolean/comparison/basic arithmetic+string
  ops; `map`/`filter`/`fold`. Result refs by **stable node id**. Raw JSON only at the boundary
  (never an unvalidated branch/loop driver). Op contracts expose these types — no schema
  language. Excluded: coercion, unions, user types, generics, functions, `while`.
- **Determinism is STRUCTURAL.** The IR exposes no clock/random/env/filesystem/network/
  live-config/callback source. Pure expressions read ONLY pinned constants, durable args,
  settled results (by node id), and derived locals → replay is sound by construction.
- **Durable workflow arguments (Step 1).** One JSON object per instance; script declares a
  closed-field arg TYPE (**the type IS in `content_hash`**; only instance VALUES are excluded);
  validated + canonicalized to **ordered-key compact canonical UTF-8 bytes** BEFORE creation;
  persisted as those bytes in an immutable child written atomically with the workflow + plan
  pin; resume reads the durable record; different content → `workflow_conflict` decided
  **byte-for-byte** (`VARBINARY`, not collated text; a hash may pre-check, not replace);
  separate from continuation / events / `security_context_ref`. (The lane's only new durable
  state.)
- **Intrinsics pin deterministic behavior** (numeric, indexing, missing-field, error) — not
  inherited from the host platform.

## Step ledger
- [~] **1 — Typed workflow IR + durable arguments** in `ir.drift` (+ `db`/`host`).
  - [x] **1a — durable argument substrate** (schema + atomic create_planned + args_get + host
    + byte-compare `workflow_conflict` + SP tests). LANDED, green.
  - [x] **1b(i) — value-model slice** (`ir.drift` type model + recursive validator + canonical
    encoding; declared argument type; validation before create; type-in-content_hash). LANDED.
  - [ ] **1b(ii) — control-flow graph** in `ir.drift` (`Operation`/`If`/`Case`/finite `Loop`/
    `Let`/`Return`); flat `PlanStep` as a degenerate straight-line graph; `content_hash` over
    the whole graph. **NEXT.**
- [ ] **2 — Semantic validation / type checking** (graph-aware): operation + compensation
  resolution · input/result contract compatibility · stable node-id result refs · arg-type
  well-formedness + references · branch target validity · typed+validated control-flow drivers
  (no raw-JSON branch/loop source) · side-effect-free + structurally-finite loop constraint ·
  reachable terminals. (Determinism needs no check — it is structural.)
- [ ] **3 — Execution: conditionals + replayable pure loops.** Pure boundaries write **no**
  continuation; restart re-derives the pure path from the last remote-op boundary.
- [ ] **4 — Pinned replay regressions across every control-flow construct** (branch, loop→op,
  mid-spine across a branch, early return, reversal across a taken branch) — asserting
  deterministic pure-control-flow replay and no continuation write at pure boundaries.
- [ ] **5 — Textual DSL parser LAST** — lowers `.mf` into the proven IR, reuses the step-2
  validator; diagnostics; possible `../drift-lang` reuse.

## Next action
**Step 1b(ii):** add the control-flow graph node types to `ir.drift`
(`Operation`/`If`/`Case`/finite `Loop`/`Let`/`Return`), re-express the existing flat
`PlanStep` plan as a degenerate straight-line graph (current suites stay green), and extend
`content_hash` over the whole graph (the value-model + arg-type + canonical machinery is
already in place). Resume-reads-`args_get` gets wired when the IR actually consumes arguments
(Step 3).

## Open questions (see README)
How far back replay restarts (last settled op vs start) · `../drift-lang` reuse for step 5.

## ✅ RESOLVED on certified 0.33.35 — recursive-IR clean form landed in `ir.drift`
Drift 0.33.35 (ABI 17, `cbf32feb`) shipped `core.Box<T>` and turned the typed-catch SIGSEGV
into a compile error. Verified + migrated:
- Re-ran the posted repro: the old `move e` form is now a clean compile error
  `E_TYPED_CATCH_BINDER_NOT_VALUE` (not SIGSEGV). A `Box<T>`-recursion + field-reconstruct
  typed-catch repro compiles and runs to exit 0.
- `ir.drift` migrated: `TArray`/`TOptional` arms `Array<IrType>` → `core.Box<IrType>`
  (`core.box(child)` / `.get()`); dropped `_one` + `_assert_well_formed`; restored a TYPED
  `catch IrError(e)` that reconstructs `IrError(message = e.message)` (`IrError` is all-scalar,
  so binder field projection is supported).

## ✅ RESOLVED — typed-catch non-scalar limitation handled via errors-as-values (0.33.35)
The 0.33.35 rebuild rejected PRE-EXISTING config code (NOT the IR lane): `build_gateway`
(singular `gateway.drift`) and `build_host` (microflows `host.drift`) re-raised a caught
`ConfigError`/`HostConfigError` with `Err(move e)`. Both errors carry a non-scalar `kind`
variant field, so on 0.33.35: the binder can't be moved (`E_TYPED_CATCH_BINDER_NOT_VALUE`), the
field can't be projected (`E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE`), and the mixed schema blocks
borrowing the scalar siblings (`E_TYPED_CATCH_BORROW_MIXED_SCHEMA`).

Toolchain team **confirmed this is an intentional v1 limitation, not a defect**
(`/tmp/drift-announce/2026-06-15T12-45-56Z-response-typed-catch-nonscalar-error-field-projection.md`),
and blessed **errors-as-values** as the recommended long-term design (NOT a workaround).
Applied: the config parsers now RETURN `Result<_, …ConfigError>` (the structured error stays a
movable native value, never round-tripping throw/catch); `build_gateway`/`build_host` use
statement-form matches (Err arm returns the value, Ok carries the config), and match
`pool.open`'s `Result` directly instead of `.or_throw()` + typed catch. The throwing leaf
helpers were replaced by Result-returning `_read_required_string`/`_read_required_int`.

Two Drift idioms hit during this: (1) expression-form `val x = match {…}` may NOT `return` from
an arm (`E_EXPR_BLOCK_MISSING_VALUE`) — use `var x = <default>; match {…}` statement-form;
(2) on 0.33.35 a match-arm binding was not in scope inside a nested `try` block — we surfaced
this, the toolchain team confirmed it as a bug, and **0.33.36 fixed it**
(`/tmp/drift-announce/2026-06-16T03-46-45Z-drift-lang-release-notes.md`). We keep the direct
`match pool.open(...) { … }` form regardless (cleaner errors-as-values; not a workaround).

**State:** full `just test` GREEN on certified **0.33.36** (and 0.33.35) — singular 16/16,
microflows 20/20 + SP 110/110, integration 67/67. The IR lane is unblocked; 1b(ii) (control-flow
graph nodes) may resume on the clean `Box<IrType>` form.
