# manual-ir-frontend — Progress / status

Charter (objective, decisions, plan, verification): [README.md](./README.md).

## Status: **Parser slices 1 + 2a + 2b-i + 2b-ii-a LANDED — straight-line + `if`/`case` + `let` + named-op/result-refs `.mf` lowers to the proven IR with content_hash + execution parity**

The lane is scoped (IR-first, parser-last). The **manual-IR runtime surface is complete and fully
proven**, and **the textual front end now covers straight-line, `if`/`case`, `let` + arg/local
expression refs, and operation naming + `result` references**: a `.mf` source lowers into the EXACT
config the manual IR already executes and hashes (a flat `"plan"` for const-only straight-line, a
control-flow `"graph"` for `if`/`case`/`let`/named-ops), with NO new runtime semantics. Full
`just test` green on certified driftc 0.33.42 / ABI 17 — integration **117/117** (was 101),
microflows 20/20, SP 110/110, singular 16/16.

Proven runtime surface (unchanged this slice): durable arguments, the typed value/argument model,
and the control-flow graph IR with EXECUTION + structural validation + graph-authoritative
`content_hash` for —
- **straight-line** operation plans (the flat plan lifted to a degenerate graph),
- **`if`** branches (uniform op-depth; branch reversal),
- **`case`** multi-way branches,
- **finite array expressions** (`map`/`filter`/`fold`, pure `IrExpr` bodies),
- **cross-branch result merge** (`NMerge` phi),
- **typed expression validation** (operation input/result contracts; three-way inference;
  assignability; compensation-payload + content_hash type identity).

Branches/loops/merges/lets write no continuation/event (only `NOperation` is a durable boundary);
replay re-derives the pure path deterministically.

**Next: parser slice 2b-ii-b** — `merge` surface syntax (`NMerge`). Then 2b-iii: finite array
expressions (`map`/`filter`/`fold`); then diagnostics/spans. Scoped below.

## Landed: parser slice 1 — straight-line textual front end (lowering parity, no new semantics)

A NEW runner-owned module `microflows/runner/src/parser.drift` lowers `.mf` SOURCE TEXT into the
SAME config the manual IR consumes — a flat `"plan"` + `"argument_type"` + per-operation
`input_type`/`result_type` contract overlays — reusing the existing `_build_plan` / `parse_graph` /
`validate_graph` / `type_check_graph` / `content_hash` machinery UNCHANGED. NO new IR nodes, NO new
execution paths, NO new durable state, NO dispatch/continuation changes. The acceptance criterion is
**lowering parity**, not a polished language.
- **Surface (slice 1, deliberately narrow).** `args { name: <type>, … }` → the closed-object
  `argument_type`; `op <name> { input: <type>  result: <type> }` → operation input/result CONTRACTS
  (both optional); `steps { <op> <json-object> … }` → a straight-line flat plan (constant JSON
  inputs, matching the proven flat-plan surface); `#`-to-EOL comments. Types: `int`/`float`/`bool`/
  `string`/`null`, `{ field: T, … }` (object), `[T]` (array), `T?` (postfix optional). No
  `if`/`case`/loop/merge/`let`/result-reference syntax yet (deferred to later slices).
- **Public API.** `parse_source(src) -> Result<ParsedWorkflow, ParseError>` (the workflow-level
  pieces: plan node, optional arg-type node, contract overlays) and `lower(src, base) -> Result<
  JsonNode, ParseError>` (merge into the base deployment routing → a complete runnable config).
  Errors-as-values: ParseError is all-scalar, caught typed at the Result boundary (mirrors
  `ir.parse_object_type`). A contract for an operation absent from the registry is a lowering error
  (a dropped contract would be absent from `content_hash`).
- **CLI.** `microflows-runner --config <base.json> --lower-source <wf.mf>` prints the merged config
  JSON to stdout, then exits — a PURE front end (reads two files, prints JSON; no DB, no claim, no
  dispatch). A malformed source (or unknown-op contract) fails HERE with a nonzero exit, before
  anything durable. The printed config is itself runnable / `--emit-content-hash`-able unchanged.
- **Tests.** Unit (`tests/unit/parser_test.drift`, DB-free, compiled with `ir.drift`+`parser.drift`,
  base+asan, gated in the runner `test` recipe): parse correctness, IR-identity parity (the parsed
  plan and a hand-authored plan lift to the same `graph_canonical`; arg/contract types match by
  `ir.canonical`), lowering overlay, and a battery of malformed-source rejections (missing/empty
  `steps`, unknown keyword/type/op-clause, unterminated braces, non-object step input, duplicate
  `args`/`op`). Integration (C11, 4 checks): a parser-lowered config and the hand-authored manual
  config produce the IDENTICAL `--emit-content-hash`; BOTH execute to the same outcome (final op
  result `{reserved:ps2}`, two dispatches each); a malformed source fails at lowering with no
  dispatch; an unknown-op contract is rejected at lowering; an emitted config referencing an op
  absent from the registry is rejected at lowering (the build-validation gate). Integration 101→106.
- **Review findings addressed (two).**
  - *Base contracts are not source identity (High).* `_merge`/`_op_strip_contracts` STRIP any
    `input_type`/`result_type` the base routing config carries from EVERY operation, then re-add ONLY
    the source-declared contracts. Without this, a `op x {}` (or a partial contract) could inherit a
    stale base type into the emitted config + `content_hash`, making source identity depend on
    non-source data. Unit `parser_test` 5b: a base with a STALE reserve contract + a contract-free
    source emits a reserve with no `input_type`/`result_type`.
  - *`--lower-source` validates the emitted config (Medium).* After lowering, the CLI runs the SAME
    build path a real run / `--emit-content-hash` uses (`_validate_registry` + `_registry_build` —
    registry/graph/type/contract/compensation checks), DB-free and dispatch-free, BEFORE printing.
    So a semantically-invalid source/config (a step op absent from the registry, a duplicate `args`
    field, a type-contract error) fails AT lowering, not later — the printed config is genuinely
    "runnable / hashable unchanged". Integration `parser_lower_validates_emitted_config`.
- **Drift notes (0.33.41).** A module-level constant is `const` (not `val`); `+`-concatenation is
  not const-evaluable (use a fn). An `export {…}` block needs a trailing `;`. Iterating
  `JsonNode.entries()` needs `use trait iter.SinglePassIterator;` in scope. `&` of a call-result
  temporary is rejected (bind to a `val` first). An empty array literal needs a type annotation. A
  bare tail `match` is not a return — bind or `return` it.

## Landed: parser slice 2a — `if`/`case` source syntax (lowers to the `"graph"` config)

`.mf` now supports structured `if`/`case`, lowering to the SAME control-flow `"graph"` the manual IR
already executes + hashes. STILL a pure front end: no new IR nodes, execution paths, durable state,
or runtime semantics; the slice reuses `parse_graph`/`validate_graph`/`type_check_graph`/op-depth/
`content_hash` unchanged. Straight-line workflows are byte-for-byte UNCHANGED (still a flat `"plan"`).
- **Surface.** `if <arg-path> { <stmt>* } [ else { <stmt>* } ]` and
  `case <arg-path> { (<json-value> { <stmt>* })* default { <stmt>* } }`. Statements nest (an `if`
  inside a `case` arm, etc.). The condition/scrutinee is a durable-ARGUMENT path (`flag`, `user.tier`)
  — the branch decision comes from durable args (the runtime requires the condition Bool / the arm
  constants to match the scrutinee type). `default` is required on `case` AND must be the LAST arm (an
  arm after it is rejected — it would lower before the default but read as a fallthrough in source);
  `else` is optional.
- **Lowering.** A small statement AST (`Stmt`/`StmtKind`/`SCaseArm`; `Array<Stmt>` recursion is fine
  — heap-indirected) is built with STABLE PRE-ORDER ids (`n0`,`n1`,… in source order, allocated at
  parse time), so a single forward pass emits `operation`/`if`/`case`/`return` nodes and the ids are
  predictable (the parity tests author the matching hand-written graph against the same scheme).
  Branch bodies re-converge at the FOLLOWING statement (the join); an empty `else`/arm/`default` flows
  straight to the join. The terminal `return` is `{const:null}` (completion uses the final durable
  op's result, exactly as the degenerate-graph straight-line case). A purely straight-line workflow
  still lowers to `"plan"` (so slice-1 output/parity is untouched); ANY `if`/`case` makes it `"graph"`.
- **Validation is the runtime's, unchanged.** Because `--lower-source` runs the real build path
  (slice-1 finding 2), an op-IMBALANCED branch (uniform-op-depth violation), a `case` missing
  `default`, a non-Bool condition, etc. are rejected AT lowering, before any dispatch.
- **Tests.** Unit (`parser_test` 8–9): an `if`/`case` source — AND a NESTED `if`-inside-a-`case`-arm
  source (8c) — lowers to a graph whose `graph_canonical` IR identity equals the hand-authored
  equivalent (the nested case also passes `parse_graph`+validate, backing "statements nest" at the
  canonical level); malformed control flow (case w/o `default`, case w/o arms, unterminated `else`,
  `if` w/o cond/body, duplicate `default`, an arm AFTER `default`) rejected. Integration (C12, 3
  checks): a parser-lowered `if`/`case` graph config and a hand-authored
  graph config produce the IDENTICAL `--emit-content-hash`; `flag:true`→branch A / `false`→branch B,
  and `mode:"a"`→arm A / unmatched→`default` (selection from durable args, asserted via the taken
  branch's checkpoint payloads); a hand-authored config executes identically; an op-imbalanced branch
  is rejected at lowering. Integration 106→109.

## Landed: parser slice 2b-i — `let` + arg/local expression references (lowers to `NLet`)

`.mf` now supports `let <name> = <expr>` (a pure-value binding → `NLet`), and operation inputs are
now EXPRESSIONS (`{…}` const object / `const <json>` / `arg <path>` / `local <name>[.path]`) rather
than constant objects only. STILL a pure front end: no new IR nodes, execution paths, durable state,
or runtime semantics; reuses `parse_graph`/`validate_graph`/`type_check_graph`/`content_hash`
unchanged. A `let` (or any non-const-object op input) makes the workflow a `"graph"`; a const-only
straight-line workflow is byte-for-byte UNCHANGED (still a flat `"plan"`).
- **Expression grammar.** `{ …json… }` → `{const: <obj>}` (the bare-object op-input shorthand);
  `const <json-value>` → `{const: <value>}` (any JSON shape); `arg <ident>(.<ident>)*` → `{arg:[…]}`;
  `local <ident>(.<ident>)*` → `{local:{name, path?}}` (path = object-field projection). Used for
  BOTH `let` values and operation inputs. Operation-RESULT references (`result`) are DEFERRED to the
  `merge` sub-slice (2b-ii) — they need an operation-naming model (node ids are parser-generated).
- **Scope is the runtime's, unchanged.** The parser does NOT track binder scope; an `ELocal` with no
  dominating binder (undefined / out-of-scope `local`) is rejected by `validate_graph` at the build
  gate (because `--lower-source` runs the real build path), before any dispatch.
- **Pure boundary.** `NLet` writes no continuation/event; resume re-derives the bound value
  deterministically from durable args/locals (proven: a fault-carrying `const` binding left an op
  requested-not-settled, and a claimable resume recomputed the SAME value → GET-first, no second PUT).
- **Tests.** Unit (`parser_test` 10): a `let p = arg req; reserve local p` source lowers to a graph
  whose `graph_canonical` equals the hand-authored equivalent; a projected `arg req.reservation`
  lowers to `{arg:[req,reservation]}` AND a projected `local p.reservation` lowers to
  `{local:{name:p,path:[reservation]}}` (both checked by graph_canonical); an undefined `local` PARSES
  but `validate_graph` REJECTS it
  (parser doesn't track scope); malformed (`let` w/o `=`, bad expression keyword, missing expr after
  `=`) rejected; a `const` scalar value binds. Integration (C13, 4 checks): a parser-lowered `let`
  graph and a hand-authored graph produce IDENTICAL `--emit-content-hash`; `let p = arg req; reserve
  local p` executes with the reserve input DERIVED from durable args; the `let` writes NO event
  (event-count parity with the no-let `reserve arg req`); a resume RECOMPUTES the bound value
  (GET-first, no second PUT); an undefined local is rejected at lowering. Integration 109→113.

## Landed: parser slice 2b-ii-a — operation naming + result references (lowers to `EResult`)

`.mf` now lets a `let` name an OPERATION's result. STILL a pure front end (no new IR nodes/runtime/
storage); lowers to a plain `NOperation` + `EResult` references, reusing the graph machinery unchanged.
- **Chosen source semantics.** `let <bind> = <op> <input-expr>` is a NAMED OPERATION: it lowers to an
  ordinary `NOperation` (parser-generated pre-order id), and `<bind>` becomes a source-stable alias
  for that op's RESULT. `result <bind>(.<field>)*` lowers to `{result:{node:<op-id>, path?}}`. The
  `let` disambiguator is the first RHS token: a `{` or an expression keyword
  (`const`/`arg`/`local`/`result`) ⇒ a pure-value `NLet` (2b-i); any other leading identifier ⇒ the
  operation name. (`let x = result r` is therefore a pure `NLet` binding a result expression.)
- **Result-name resolution.** A parser symbol table maps result-name → the named op's node id. A
  reference to an UNKNOWN name (or a FORWARD reference) is a parse error; a DUPLICATE result name is a
  parse error (the table can't resolve two). Because the alias lives ONLY in the parser (never in the
  IR), result refs are STABLE under source formatting AND under renaming the alias — the emitted graph
  (and `content_hash`) is identical.
- **Scope is the runtime's, unchanged.** The parser does NOT enforce dominance; a CROSS-BRANCH result
  ref without a merge (a name bound in one branch, referenced after the join) resolves in the parser
  but `validate_graph` REJECTS it at the build gate (`EResult` must be dominated) — before any dispatch.
- **Tests.** Unit (`parser_test` 11): `let r = reserve {…}; confirm result r` lowers to a graph whose
  `graph_canonical` equals the hand-authored equivalent (`result r` → `{result:{node:n0}}`); reformat
  + rename `r`→`myRes` yields the IDENTICAL identity; unknown / duplicate result name → parse error; a
  cross-branch ref PARSES but `validate_graph` rejects it; `let x = result r` parses. Integration
  (C14, 4 checks): parser-lowered and hand-authored graphs produce IDENTICAL `--emit-content-hash`,
  both execute, and a named op's RESULT feeds the downstream `confirm` input (the durable confirm
  op input.reserved == the reserve result); result refs are STABLE under formatting/alias-rename
  (same `--emit-content-hash`); a cross-branch ref is rejected at lowering; a RESUME recomputes the
  result-derived value from the durable operation result (op set unchanged, trailing op GET-first, no
  second PUT). Integration 113→117.

## Deferred: later parser sub-slices (each lowers to already-proven IR; no new runtime semantics)
- **Slice 2b-ii-b:** `merge` surface syntax (`NMerge`; selects a branch-local op result at a join to
  feed shared downstream work). Lowers to the `"graph"` config; same content_hash/execution parity;
  merge writes no event; resume recomputes the merged value; reversal compensates only the taken
  branch + shared downstream op (matching the manual-IR C9 proof).
- **Slice 2b-iii:** finite array expressions (`map`/`filter`/`fold` → `NLoop`) surface syntax.
- **Slice 3+:** diagnostics/spans (currently shallow — byte offsets), and possible `../drift-lang`
  reuse for the lexer/parser/type-checker.

## Landed: control-flow graph IR — chunk 1 (Step 1b(ii): types + canonical + flat compat)
- **Graph node types in `ir.drift`** (additive; runner behavior unchanged): `IrExpr` (closed leaf
  source set — `EConst`/`EArg`/`EResult`/`ELocal`, each path-projectable; determinism is
  structural), `LoopKind` (`LMap`/`LFilter`/`LFold` — finite array expressions, no `while`),
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
  derives `plan_length` from **`ir.op_depth`**. (At this chunk it also rejected `NLoop`; loops are
  now executed as finite array expressions — see that section below.) The forward loop drives
  `ir.advance` for `operation`/`if`/`let`/`return`.
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
- **Deferred (then):** finite loops, `case`, cross-branch result merge, the source-language parser.
  (Finite array expressions — `map`/`filter`/`fold` — are now EXECUTED; see that section below.)
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

## Landed: manual-IR finite array expressions — map / filter / fold (NLoop execution)

`NLoop` is now EXECUTED in `ir.advance` for the restricted V1 model: finite, pure, local-only
array expressions whose result feeds later operation inputs. NOT workflow-loop semantics — a loop
is a pure VALUE step (like `let`), never a durable progress step. Parser, `while`, node-graph
bodies, and remote ops in iteration remain deferred.
- **Model.** The loop result binds to a downstream local named by config key `as` (the `acc`
  field). For `fold` it is ALSO the in-body accumulator (threaded from `init`); for `map`/`filter`
  it is only the post-loop result name (their `init` is the unit null). No `NLoop` signature change
  — so `graph_canonical` / `content_hash` / `flat_to_graph` are structurally untouched.
- **Execution (`_eval_loop`).** Source must evaluate to a finite array (else a clean `Fault`, no
  panic). `map` → array of body results; `filter` → the ORIGINAL elements whose (bool) body is true
  (non-bool body → `Fault`); `fold` → the accumulator threaded `init`→body→…. The body is a pure
  `IrExpr` over `elem` (+ `acc` for fold) — a remote dispatch is structurally impossible.
- **Config + strictness.** New node kinds `map`/`filter`/`fold` in `parse_graph` (exact allowed
  keys: `{kind,id,source,elem,as,body,next}`, fold adds `init`; unknown/missing keys rejected).
  `validate_graph`: `as` required + distinct from `elem`; map/filter `init` must be null; a loop
  result is a dominating local binder (downstream `ELocal(as)` resolves; globally unique with NLet
  names). `_assert_executable` no longer rejects `NLoop` (loops contribute 0 to `op_depth`).
- **Durable semantics unchanged.** A loop is not an `NOperation`: no operation seq, no continuation
  write, no event at the loop boundary. Replay recomputes the loop deterministically from durable
  args/results/locals.
- **Tests.** Unit (`ir_exec_test`): map/filter/fold success (incl. empty source), source-not-array
  + non-bool-filter Faults, fold threads `init` and writes per element, a loop feeding an operation
  input + resume recompute, and validation/parse strictness (elem≠as, non-null map/filter init,
  valid map/fold parse, unknown/missing key rejects). Integration (C7): a `fold` computes an
  operation input and the op dispatches EXACTLY once; event-count parity with a const-input op
  proves NO durable event at the loop boundary; a claimable RESUME recomputes the loop-derived input
  identically (settled prior op stays settled + not re-dispatched, later op GET-first, no second
  PUT). Integration 84/84 (was 81); full `just test` green.
- **The manual graph now supports:** straight-line, `if`, `let`, `return`, and finite array
  expressions (`map`/`filter`/`fold`). Deferred: source-language parser, `while`, node-graph loop
  bodies, remote operations inside iteration, `case` config surface.

## Landed: manual-IR control flow — `case` (NCase from graph config)

`NCase` is now reachable from the graph config. The interpreter, structural validation, `op_depth`,
and reversal already supported it (`_succs` includes every arm target + default, so all the
execution-model invariants apply unchanged); this slice adds ONLY the config surface + hardening.
Pure control flow: no durable boundary, no continuation/event.
- **Config.** `{"kind":"case","id":...,"scrutinee":<expr>,"arms":[{"match":<json>,"target":...},...],
  "default":...}`. Strict keys (case node = 5; each arm = exactly `{match,target}`); `default` is
  REQUIRED. The arm `match` is canonicalized into the match constant (so `{"a":1,"b":2}` and
  `{"b":2,"a":1}` collapse to one arm). `validate_graph` (unchanged) enforces canonical/valid match
  constants and rejects DUPLICATE arm constants; replay takes the first matching arm else `default`.
- **Execution model preserved.** `op_depth` stays uniform across every arm AND the default (an
  op-unbalanced case is rejected at build → `invalid_config`); `nonfinal_operations` enforces
  compensation across all reachable paths, so a forward failure reverses only the taken arm's
  checkpoints.
- **Tests.** Unit (`ir_exec_test`): valid case parses+validates; duplicate arm constants (different
  spelling, same canonical) rejected; missing `default` / unknown arm key / missing `match` /
  unknown node key rejected. Integration (C8): matching arm selected from durable args; default arm
  when no match; no durable event at the case boundary (event-count parity with a 1-op plan);
  claimable RESUME replays the same selection from durable args (else it would FAULT, not stay
  pending); op-unbalanced case rejected before dispatch; branch reversal compensates ONLY the taken
  case path (untaken arms have no op row/checkpoint/execution). Integration 90/90 (was 84); full
  `just test` green.
- **The manual graph now supports:** straight-line, `if`, `case`, `let`, `return`, and finite array
  expressions (`map`/`filter`/`fold`).

## Landed: manual-IR cross-branch result merge (NMerge phi)

Branch-local values (an op result / let bound on one branch only) can now feed shared downstream
work, WITHOUT inventing storage. A new `merge` node (an SSA phi) rejoins paths and binds a local to
a value selected from the TAKEN branch. Pure control flow: no operation seq, no continuation write,
no event at the merge boundary; the value is recomputed on replay from durable args/results/locals.
- **Model (`NMerge(id, name, sources, next)`).** Each `MergeArm{from, value}` keys a value to the
  immediate predecessor it arrives from. `advance` tracks the predecessor (`prev`) and, at the
  merge, picks the source for `prev`, evaluates it in the taken-path scope, and binds local `name`
  (like an NLet). The EXISTING `EResult`/`ELocal` dominance rule is unchanged — the merge is the
  EXPLICIT, validated mechanism for crossing a branch; a bare cross-branch reference is still
  rejected.
- **Validation.** A merge must be TOTAL and UNAMBIGUOUS over its incoming edges: every immediate
  predecessor has exactly one source, and every source's `from` is a predecessor (missing /
  non-predecessor / duplicate `from` → rejected). Each source's value is scoped to its predecessor
  (`_available_after`: the predecessor's own result/binding, or a dominator's), so a source can
  reference the branch op result it joins but NOT the other branch's. `name` is a normal binder
  (globally unique with NLet/loop results; downstream `ELocal(name)` resolves via dominance). Binder
  helpers were unified behind `_binder_name_of` (NLet / NLoop result / NMerge result). op-depth
  imbalance across arms remains rejected (no durable model for variable op counts yet).
- **Tests.** Unit (`ir_exec_test`): merge selects the taken branch's result (true + false), and
  rejects a missing-source merge, a cross-branch source value (`result(other)`), an unknown source
  key, and a missing `from`; valid merge parses+validates. Integration (C9, via a new `confirm` op
  whose input is a reserve RESULT, compensable → `unconfirm`): a branch reserve RESULT is merged
  into the shared `confirm` input (true→mgA / false→mgB); event-count parity proves NO durable event
  at the merge boundary; a claimable RESUME recomputes the merged value identically from durable
  results (settled ops untouched, trailing op GET-first, no second PUT); branch reversal compensates
  the taken branch (`release`) AND the shared downstream checkpoint (`unconfirm`), highest-seq first,
  with the untaken branch untouched. Integration 94/94 (was 90); full `just test` green.
- **The manual graph now supports:** straight-line, `if`, `case`, `let`, `return`, finite array
  expressions (`map`/`filter`/`fold`), and cross-branch result merge.

## Landed: typed expression validation (operation contracts + graph type checking)

Operations now carry OPTIONAL contracts in the existing IR value-type model — an input type and a
result type — and the runner TYPE-CHECKS the graph at registry build, before any claim/dispatch.
This is the last substantial runtime-side frontend hardening before the parser.
- **Contracts (`ir.OpContract`).** `{name, input_type?, result_type?}`, built from each config
  operation's `input_type`/`result_type` (a value-type encoding). Both OPTIONAL: an op that declares
  neither is UNCHECKED, so every existing untyped config behaves exactly as before. The declared
  types fold into `content_hash` (tagged, appended only when present — untyped configs keep their
  current hash, so the seeded flat-plan pins are unchanged).
- **`ir.type_check_graph(g, arg_type, contracts)`** — a topological pass (binders typed before use).
  Inference is THREE-WAY (`InferType`): `Known` (checked by assignability), `Unknown` (only from an
  UNTYPED op result/binder — the backward-compatible permissive escape hatch), and `Imprecise` (a
  const literal whose precise type is undetermined — empty/heterogeneous array, or an object/array
  containing one). `Imprecise` is NEVER permissive: at a typed op input a STATIC const (a literal, or
  one reached through a `let`, projected) is checked by VALUE (`validate`, exact ground truth); an
  imprecise const reaching the boundary through a non-static path (`merge`/`loop`) is conservatively
  REJECTED. So the bypass — a bad const laundered through any binder — is closed. Checks: `EArg`
  resolves against the argument type; `EResult` against the op's result type; `ELocal` against its
  let/loop/merge bound type (all by path projection through object fields, unwrapping `Optional`);
  `NIf.cond` is Bool; `NCase` arm constants `validate` against the scrutinee type; `NLoop` source is
  `Array<T>` with the body checked (filter body Bool; `map` result `Array<body>`; `fold` body == the
  accumulator type — a `null` init is a BOTTOM accumulator, e.g. fold-select-last, so the result is
  the body type); `NMerge` sources must agree on one type. Type equality is `canonical()` equality;
  consts are checked by VALUE via the existing `validate()`. CLOSED object const literals are
  inferred field-by-field (`_infer_const_type` walks `node.entries()`), so a bad-typed const reaching
  an op input THROUGH a `let`/`merge`/`loop` binder is still caught — not just direct const inputs.
- **Compensation contracts.** A compensation receives the forward op's CHECKPOINT PAYLOAD (its
  input) as its own input. So the reverse op's declared input type must MATCH the forward op's
  (`_assert_compensation_types`), and the compensation op's type tags are folded into the
  `content_hash` (lowercase tags, appended only when present) — a changed reverse contract changes
  the revision identity, never a silent substitution.
- **Runtime unchanged / surfacing.** A bad type contract throws `RunnerError` at registry build →
  `invalid_config` (submission) / `revision_unavailable` (resume), exactly like the other build-time
  rejections — never a dispatch, no new durable state. (A typed `catch` can't project `ir.IrError`'s
  `message`, so `_op_type_opt` uses a catch-all re-raise.)
- **Tests.** Unit (`ir_exec_test` 140–151): valid typed graph; Int branch condition rejected; bad op
  input (const int vs declared string) rejected; merge type mismatch rejected; a bad const reaching a
  typed input through a `let` / `merge` / `loop` binder rejected (incl. an imprecise-field object and
  an optional/null field); non-const assignability (`{m:string}` ⊑ `{m:Optional<string>}`); and a
  non-array loop source rejected — a direct literal AND one laundered through a merge. Integration
  (C10, via per-test `typed_graph_cfg`
  overlaying contracts so the global registry stays untyped): valid typed graph still executes;
  invalid branch condition / invalid op input / invalid merge mismatch each rejected before dispatch
  (`invalid_config`, exec 0); resume under a CHANGED result-type contract yields
  `revision_unavailable`; compensation input incompatible with the checkpoint payload rejected; a
  changed COMPENSATION type yields `revision_unavailable` (the type is in the hash — no substitution).
  Integration 101/101 (was 94); full `just test` green.
- **The manual graph now supports:** straight-line, `if`, `case`, `let`, `return`, finite array
  expressions, cross-branch result merge, AND typed expression validation. The manual-IR surface is
  now "boring and fully proven." Deferred until parser/DSL: the source-language parser, `while`,
  node-graph loop bodies, remote operations inside iteration, and a durable model for variable
  per-branch op counts.

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
  single source for validation, identity (content_hash), and forward execution. (Non-degenerate
  control-flow EXECUTION — branches, `let`, and finite array expressions — has since LANDED; only
  the source-language parser remains.) The IR types + validation + interpreter already exist in
  `ir.drift`, exercised by
  `ir_exec_test`, but are not yet reachable from config and not yet executed by the runner).**

## Landed: graph validation + interpreter — chunk 2 PART 1 (ir.drift; runner not yet switched)
Authoritative validation + a pure-control-flow interpreter, both in `ir.drift`, fully unit-tested.
The runner does NOT yet build/validate/execute via the graph and `content_hash` is UNCHANGED —
that adoption (+ fixture recompute + restart integration tests) is chunk 2 PART 2.
- **`validate_graph(g) -> Optional<String>`** — rejects: empty graph, duplicate ids, missing
  `entry`, dangling edge targets, control-flow CYCLES (DFS; loops are finite array expressions, not
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
  same step. Persistence unchanged — only `NOperation` is a durable boundary. (Loop EXECUTION has
  since LANDED — `advance` evaluates `map`/`filter`/`fold` as pure value steps; see the finite
  array expressions section.)
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
- **V1 loops are structurally finite array expressions** (`map`/`filter`/`fold` over a finite
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
- [x] **1 — Typed workflow IR + durable arguments** in `ir.drift` (+ `db`/`host`).
  - [x] **1a — durable argument substrate** (schema + atomic create_planned + args_get + host
    + byte-compare `workflow_conflict` + SP tests). LANDED, green.
  - [x] **1b(i) — value-model slice** (`ir.drift` type model + recursive validator + canonical
    encoding; declared argument type; validation before create; type-in-content_hash). LANDED.
  - [x] **1b(ii) — control-flow graph** in `ir.drift` (`Operation`/`If`/`Case`/finite `Loop`/
    `Let`/`Merge`/`Return`); flat `PlanStep` as a degenerate straight-line graph; `content_hash`
    over the whole graph. LANDED (see the chunk sections above).
- [x] **2 — Semantic validation / type checking** (graph-aware): structural `validate_graph`
  (unique ids, edges, acyclic, reachable, terminal shape, dominance for `EResult`/`ELocal`,
  loop/merge shape) + `type_check_graph` (operation input/result contracts, `EArg`/`EResult`/
  `ELocal` path resolution, `NIf` Bool, `NCase` arms, `NLoop` source/body, `NMerge` agreement,
  assignability, compensation-payload compatibility). LANDED.
- [x] **3 — Execution: conditionals + replayable pure loops + merge.** `ir.advance` drives
  `operation`/`if`/`case`/`let`/`map`/`filter`/`fold`/`merge`/`return`. Pure boundaries write
  **no** continuation/event; restart re-derives the pure path from the last remote-op boundary.
  LANDED.
- [x] **4 — Pinned replay regressions across every control-flow construct** (branch true/false,
  case selection + default, loop-derived op input, merge into a shared op, mid-flight resume,
  reversal across a taken branch/case + shared downstream, changed-contract → revision_unavailable)
  — deterministic pure-control-flow replay, no continuation write at pure boundaries. LANDED
  (integration C6–C10).
- [~] **5 — Textual parser** (NEW sub-step) — lowers source text into the proven IR, reusing
  `_build_plan`/`parse_graph`/`validate_graph`/`type_check_graph`/`content_hash` with NO new runtime
  semantics.
  - [x] **slice 1 — straight-line** workflow + declared arguments + operation input/result contracts
    → `microflows/runner/src/parser.drift` + `--lower-source` CLI → emits the same `plan`/
    `argument_type`/contract config → `content_hash` + execution parity with the hand-authored manual
    IR (unit `parser_test`; integration C11). LANDED, full `just test` green.
  - [x] **slice 2a — `if`/`case`** source syntax → lowers to the `"graph"` config (pre-order node ids;
    branch/case bodies re-converge at the join; selection from durable args). content_hash + execution
    parity with hand-authored graph configs; op-imbalanced/malformed rejected at lowering (unit
    `parser_test` 8–9; integration C12). LANDED, full `just test` green (integration 109).
  - [x] **slice 2b-i — `let` + arg/local exprs** → `let <name> = <expr>` lowers to `NLet`; operation
    inputs become expressions (`{…}`/`const`/`arg`/`local`). content_hash + execution parity; `let`
    writes no event; resume recomputes from durable state; undefined local rejected at the build gate
    (unit `parser_test` 10; integration C13). LANDED, full `just test` green (integration 113).
  - [x] **slice 2b-ii-a — operation naming + result refs** → `let <bind> = <op> <input>` names an op
    (plain `NOperation`); `result <bind>` → `EResult` via a parser symbol table. Parity + execution;
    result refs stable under formatting/alias-rename; unknown/duplicate/cross-branch refs rejected at
    parse or the build gate; resume recomputes from durable results (unit `parser_test` 11; integration
    C14). LANDED, full `just test` green (integration 117).
  - [ ] **slice 2b-ii-b —** `merge` syntax (`NMerge`).
  - [ ] **slice 2b-iii —** finite array expressions (`map`/`filter`/`fold` → `NLoop`) syntax.
  - [ ] **slice 3+ —** diagnostics/spans; possible `../drift-lang` reuse.

## Next action
**Step 5, parser slice 2b-ii-b:** `merge` surface syntax → `NMerge`. With operation naming + `result`
refs now in place (2b-ii-a), add source syntax for a phi that rejoins branches, binding a name to a
value selected by which branch was taken (so a branch-local op result can feed shared downstream
work). Lowers to `NMerge` + `MergeArm{from,value}`. Same acceptance: parser-emitted vs hand-authored
graph → identical `--emit-content-hash` and run outcome; merge writes no event; resume recomputes the
merged value from durable results; reversal compensates only the taken branch + shared downstream op
(matching the manual-IR C9 proof). No runtime/IR/storage changes (front end only).

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
