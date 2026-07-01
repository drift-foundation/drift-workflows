# Workflow Composition — Progress

## Status

Release is cut + announced. Design at `DESIGN.md` is at **rev 3** (preflight re-slice + K review rounds folded
in). **Decision #1
(transport) RESOLVED → internal durable host API, async + awaited** — a workflow call is a pending **call
operation**; the parent stays in normal forward execution, and a blocked child does **not** cascade up the
call tree. **Decision #2 (slice plan) RE-SLICED by preflight** — 1a frontend-only, 1b forward spine (no
compensation runtime), 1c compensation. Terminology fixed (workflow call / child workflow / call operation;
avoid "callback").

**Slice plan (re-sliced by preflight):**
- **Slice 1a** — frontend only (no DB/runtime): grammar + IR `NCallWorkflow` + contract validation + **static
  cycle check** + mermaid. Parses `compensation` but **build-rejects it until 1c** (never accept-and-ignore).
- **Slice 1b** — forward async-call spine, **no compensation runtime**: sidecar `tb_mf_call` + `call_kind`;
  `call_submit` / `call_inspect` (authoritative child read) / `child_terminal_notify` (wake + status-hint
  only) / settle / recovery / recursion-guard. No-comp reversal = no-op. **Minimal inspectability**
  (`child_workflow_id` + `child_status` so operators follow A→B→C). **No blocked cascade.**
- **Slice 1c** — compensation path: reconsider compensating-workflow vs reverse-child/T1, then ship one.
- **Slice 2** — stuck-child liveness budget (standalone). **Slice 3** — fan-out + `on failed`-as-data.

The per-slice **build checklists** (1a/1b/1c) are in `DESIGN.md`.

**Design-review pass folded in (5 findings):** (1) the runtime **recursion guard** now keys on the
**ancestor SET of plan-identity keys** `(script_name, plan_version, content_hash)` — child ids are freshly
derived per call, so an instance-id ancestor check can't catch an A→B→A cycle; plus `max_call_depth`. The key
needs **no new workflow column** (`content_hash`/`plan_version` are already in `tb_mf_workflow_plan`).
(2) The **call-operation storage model** is now concrete: `operation_id = child_workflow_id`;
`schema_version`/`status`/`result_json` keep their existing meaning + invariant (NOT overloaded with the
child plan revision); child plan identity lives in call-specific columns `child_script_name` /
`child_plan_version` / `child_content_hash`; `sp_mf_call_submit` is a **sibling** of
`sp_mf_operation_request` (not a caller). (3) `child_terminal_notify` is **wake/stage-only** — the runner +
`call_inspect` is the single authoritative settle/reversal. (4)/(5) Stale open-decision sections retired and
the durable-state sketch marked slice-neutral (liveness columns = slice 2).

**Second review round folded in (storage/identity hardening):** (1) **`call <child>@<plan_version>`** is now
explicit — `@` is a **semantic plan version** (`major.minor.patch`), registry-resolved by exact-match to the
pinned `content_hash` (the same plan-pin model as a top-level workflow); not an opaque alias, not a
participant `schema_version`. (2) The call op's **`operation_name = child_script_name`** (no prefix → no `varchar(128)`
overflow; **`call_kind`** is the discriminator; `sp_mf_operation_settle` copies it into the checkpoint, so
it is the compensation envelope's `forward.operation`). The call op's `schema_version` is a named constant
**`CALL_OPERATION_SCHEMA_VERSION = 1`**, and the comp **`{forward:{…}}`** envelope carries the full
correlation set (`workflow_id`, `operation`, **`operation_id = child_workflow_id`**, `schema_version`,
`input`, `result`). Liveness is **split**: slice 1b ships terminal push + poll fallback; the stuck-child
budget is slice 2 (standalone). (3) The compensation workflow is pinned by its **exact plan identity** (`comp_script_name`
/ `comp_plan_version` / `comp_content_hash`), mirroring the checkpoint's `reverse_operation_name`+
`reverse_schema_version` pin — not a loose `name@rev`. (4) The recursion ancestor set is **reconstructed by
walking `parent_workflow_id` links + joining `tb_mf_workflow_plan`** (bounded by `call_depth`) — no
denormalized ancestor column. (5) Doc title aligned with this index.

**Preflight architecture review folded in (complexity-containment + reliability lens).** Five structural
changes before any parser code: (1) **Re-sliced** to 1a (frontend) / 1b (forward spine) / 1c (compensation) —
because compensating-workflow in slice 1 hid a *second* async-call-await/settle/recovery engine, not obviously
simpler than reverse-child/T1. (2) **Slice 1 runtime is NO-COMP only**; 1a parses `compensation` but **build
validation rejects it** until 1c (never accept-and-ignore). (3) **Minimal inspectability in 1b** — the parent
call op exposes `child_workflow_id` + last `child_status` so operators follow A→B→C by hand (no blocked
cascade). (4) **Notify is wake + status-hint ONLY** — no `child_return_json` value-of-record; `call_inspect`
re-reads the child's authoritative terminal at settle; **poll is the floor**, correctness never depends on
notify (also makes a future T1 reopen safe). (5) **Sidecar `tb_mf_call`** holds all composition state;
`tb_mf_operation` gains only the `call_kind` discriminator (don't widen the hot op table). Also recorded: the
**app-boundary lens** — the service may take the complexity, but business apps/participants/authors must never
handle plan hashes, parent links, leases/fences, notify/poll mechanics, or checkpoint state.

## Current Scope

**Slice 1a is mainlined** (commit `62555a4`: `call` grammar → `NCallWorkflow` IR, structural validation,
build-block of reachable calls + `compensation`). **Active scope: slice 1b — forward async-call spine, no
compensation runtime.** Sub-order (decided): **1b.0a** (workflow return contract) → **1b.0** (registry
validation gate) → **1b.1** (IR build-block inversion + schema/SP/host + runner) → **1b.2** (acceptance).

### 1b.0a status (in progress) — **workflows are typed functions** (decided)
- **`return <expr>` statement added to the parser** but **PARSE-GATED** (rejected like `fan`/`on failed`)
  until the full typed-return contract lands — the lowering (`KReturn`→`_return_value_node`) is kept dormant.
  Gate fixture: `err_return_unsupported`. Parser gate 99/99.
- **Decisions RESOLVED** (was the open storage question): **object-only or unit** workflow returns
  (unit ⇒ `{}`); a **durable workflow-terminal-return store** holds the evaluated return **separate** from
  per-op results; it is written **atomically with completion** (the final settle writes final-op result +
  workflow return + `state=completed` in ONE fenced txn; `sp_mf_operation_settle` takes `workflow_return_json`
  when `is_final=1`); **terminal replay reads the stored return — NOT graph re-derivation**; hash policy:
  absent≡unit, unit = empty suffix (existing hashes unchanged), non-unit = `ir.canonical(return_type)`.
- **1b.0a build plan (each gated):** **(1) IR return-contract validation — DONE** (`ir.validate_return_contract`;
  object-or-unit type check + non-unit ⇒ every successful sink is an explicit `return`, implicit unit
  fall-through rejected, `fail` exempt; `ir_exec_test` base+asan `exit=0`, `ir_graph_test` base `exit=0`).
  **Deferred within step 1 (must land before un-gate):** the per-expression **structural** check that an
  explicit non-unit `return <expr>` is object-shaped / matches `return_type` — step 1 enforces terminal-shape
  only, so "object-only returns" are NOT yet fully validated. Remaining: (2) `returns.type` in config +
  content_hash; (3) durable return store + atomic final settle (schema/SP); (4) runner finality probe passes
  `Completed(result)` (today **discarded** at `runner.drift:~1773` — reports the last op result); (5) terminal
  replay from the stored return; (6) child-call result binding (in 1b.0) + the deferred structural expr check;
  then un-gate.

Overall feature: any workflow step may be a child workflow; the child owns its durable state; the parent
treats the call as one forward step/checkpoint with result data flow; compensations may be workflows (1c);
fan-out uses stable child ids (slice 3).

## Verification

**Slice 1a frontend core — IMPLEMENTED + verified (registry-free part).**
- `parser.drift`: `[let x =] call <child>@<plan_version> { <input> } [compensation <wf>@<plan_version>]`
  parsed → `KCall` AST → lowered to a `call` graph node; `@<plan_version>` is a `maj.min.patch` token;
  `on failed` / `fan` rejected as "not in this release".
- `ir.drift`: `NCallWorkflow` node (+`CallComp`) — `parse_graph` branch, canonical (`W` tag, hashes the
  compensation), mermaid, `_succs`/`_node_id`/`_check_node` arms, `advance` defensive-fault (no 1a runtime),
  `EResult` may now reference a call node, and `validate_graph` **build-rejects `compensation`** until 1c.
- Compiles clean (standalone `driftc` over `parser.drift`+`ir.drift`); a smoke driver passes (valid call ok;
  compensation rejected; `on failed`/`fan`/bad-version/non-object-input all parse-rejected).
- 7 new `--parse-check` fixtures + goldens under `runner/tests/fixtures/parser/check/` (`call_single`,
  `call_bare`, `call_compensation_rejected`, `err_call_{on_failed,fan,bad_version,no_input}`).
- **No regression:** a faithful standalone `_parse_check` replica reproduced all **88** existing check
  goldens byte-for-byte. `ir_graph_test` passes. The later refreshed environment note below supersedes the
  old local-build caveat: runner + integration builds now run locally with the certified package root.

**Manifest-backed validation — now slice 1b.0 (build-time registry gate), not a 1a remnant:** child
`name@<plan_version>` registry resolution + input↔child-`arg` / downstream↔child-`return` contract match +
the **static recursion/cycle check** (enumerate each pinned plan's `NCallWorkflow` edges). These live in the
`--manifest` path (`runner.drift`) — the only place a multi-script registry exists — and are **buildable +
testable locally** now (see *Current reality*); the earlier "not testable locally" caveat is retired.

## Current reality (refreshed)

- **Slice 1a is mainlined** at commit `62555a4` (`call` grammar → `NCallWorkflow` IR + structural validation +
  build-block of reachable `call`/`compensation`); the registry-free frontend core is committed, not dirty.
- **`return <expr>` is parse-gated** at commit `9195854` — the statement is recognized but `_parse_return`
  throws `unsupported-in-release` (gate fixture `err_return_unsupported`); the `KReturn → NReturn` lowering is
  kept dormant.
- **Local runner + integration build is now available** with the correct package root:
  `DRIFT_TOOLCHAIN_ROOT=~/opt/drift/certified/current/toolchain DRIFT_PKG_ROOT=~/opt/drift/certified/current/pkgs`.
  The full runner binary builds from source and the coordinator↔singular integration gate runs locally — the
  earlier "not buildable/testable locally (missing deps)" caveat is **stale**.
- **Slice 1b.0a step 1 (IR return-contract validation) is DONE + verified** — `ir.validate_return_contract`
  (object-or-unit + non-unit ⇒ explicit-`return` on every successful path; `fail` exempt); `ir_exec_test`
  base+asan `exit=0`, `ir_graph_test` base `exit=0`; `return` parse-gate untouched. **Active scope: step 2**
  (`returns.type` config + content_hash plumbing).

## Step 1 — DONE (IR return-contract validation)

`ir.validate_return_contract(g, return_type: Optional<IrType>)` (standalone; not yet wired into
`validate_graph`). Exact behavior: **unit** (`None`) accepts any terminal shape; a **non-unit** type must be
an **object** (else rejected — object-only); for a non-unit type **every successful sink `NReturn` must be an
explicit `return`** (value ≠ the implicit unit literal `null`) — an implicit unit fall-through on any
successful path is rejected (stricter than "no reachable return"); **`fail` (NFail) is exempt** (a
fail-only workflow is vacuously accepted). **Gate:** `ir_exec_test` base+asan `exit=0` (checks 170–177),
`ir_graph_test` base `exit=0`; `return` parse-gate untouched. **Deferred (before un-gate):** the per-expression
structural check that an explicit non-unit `return <expr>` is object-shaped / matches `return_type` — so do
NOT describe object-only returns as *fully* validated until that lands.

## Literal Next Action — 1b.0a step 2 (`returns.type` config + content_hash)

Source the declared `return_type` from the manifest contract (`returns: { type: <type> }`, mirroring
`arguments: { type }`), parse it via `ir.parse_type` (must be an object type or absent≡unit), and **fold it
into `content_hash`** per the locked hash-compat rule: **absent ≡ unit ⇒ empty/identity suffix** (existing
hashes unchanged), **non-unit ⇒ `ir.canonical(return_type)`**. Then call `validate_return_contract` from the
build path with that type. Still **no parser un-gate, no runner/storage** (steps 3–6). Wording guard: this
step adds the type to identity + wires the terminal-shape check — it does **not** add the deferred structural
expression check.

Concrete touchpoints in `microflows/runner/src/runner.drift` for resume after restart:
- Add `return_type: Optional<ir.IrType>` to `ScriptRevision` next to `arg_type`.
- Add a helper parallel to `_arg_type(cfg)` that reads config `returns.type`:
  absent `returns` or absent `returns.type` ⇒ `Optional::None` (unit); present type ⇒ `ir.parse_type`, then
  require object type or reject with `RunnerError`.
- In `_registry_build`, compute `return_type` after `arg_type`, call `ir.validate_return_contract(&graph,
  &return_type)` after `ir.validate_graph` / type-check is otherwise ready, and store it in `ScriptRevision`.
- Change `_content_hash(cfg, graph, arg_type)` to accept `return_type`; keep the existing bytes identical for
  unit (`None`) by appending **nothing** for unit, and append `ir.canonical(return_type)` only for non-unit.
- `_emit_content_hash` and manifest loading already go through `_registry_build`; add/adjust fixtures/tests
  around those paths to prove absent/unit hashes stay unchanged and non-unit return type changes the hash.
- Do not touch parser un-gating, durable storage, final settle, or terminal replay in this step.

Then step 3 (durable return store + atomic final settle) → 4 (finality probe passes `Completed(result)`) →
5 (terminal replay from stored return) → 6 (child-call result binding + the deferred structural expr check)
→ un-gate `return`; then 1b.0 registry gate, 1b.1 runtime spine, and 1c per the `DESIGN.md` checklists.
