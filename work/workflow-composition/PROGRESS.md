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
  **(2) `returns.type` config + content_hash — DONE** (`ScriptRevision.return_type` + `_return_type(cfg)` +
  `_content_hash` suffix + `validate_return_contract` wired into `_registry_build`; `_return_type` also
  validates the `"returns"` wrapper itself — must be a JSON object with only the `type` key, else
  `invalid_config` (a non-object wrapper or a typo'd key like `"types"` is rejected, not silently unit —
  review-flagged and fixed same step); runner unit tests green; 7 new coordinator-singular integration
  checks, 215/215 green).
  **Deferred within steps 1–2 (must land before un-gate):** the per-expression **structural** check that an
  explicit non-unit `return <expr>` is object-shaped / matches `return_type` — terminal-shape only so far, so
  "object-only returns" are NOT yet fully validated. Remaining: (3) durable return store + atomic final settle
  (schema/SP); (4) runner finality probe passes `Completed(result)` (today **discarded** at
  `runner.drift:~1773` — reports the last op result); (5) terminal replay from the stored return; (6) child-call
  result binding (in 1b.0) + the deferred structural expr check; then un-gate.

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
  base+asan `exit=0`, `ir_graph_test` base `exit=0`; `return` parse-gate untouched.
- **Slice 1b.0a step 2 (`returns.type` config + content_hash) is DONE + verified** — `ScriptRevision` gained
  `return_type: Optional<ir.IrType>`; `_return_type(cfg)` reads `returns.type` (absent `returns`, or `returns`
  present with `type` absent — i.e. exactly `{}` — ⇒ unit; present `type` ⇒ `ir.parse_object_type`,
  object-only, else `RunnerError`); `_registry_build` computes it after `arg_type`, calls
  `ir.validate_return_contract`, and folds it into `_content_hash` via `_return_type_hash_suffix` (unit ⇒
  empty suffix, byte-identical to every pre-existing hash; non-unit ⇒ `ir.canonical(return_type)`).
  **Wrapper strictness (review-flagged, fixed same step):** `_return_type` also requires `returns` itself
  be a JSON object with EXACTLY its allowed key (`type`, or none for unit) — a non-object `"returns"` (e.g.
  `"returns": "bad"`) or an unknown key inside it (e.g. a typo'd `"types"`) is a `RunnerError`
  (`invalid_config`), never silently defaulted to unit; this mirrors the unknown-key rejection every other
  type declaration in the file already enforces. `just test` (runner) green: `ir_graph_test`/`ir_exec_test`
  base+asan, 99/99 parser fixtures, full binary build. `coordinator-singular` integration green: 215/215
  (7 new checks — absent≡unit hash identity via an explicit-but-typeless `returns` block, non-unit changes
  the hash, a non-object `returns.type` is `invalid_config`, non-unit + implicit fall-through is
  `invalid_config`, a non-unit graph with every path explicit-`return`ing builds + runs, a non-object
  `returns` wrapper is `invalid_config`, and an unknown key inside `returns` is `invalid_config`). Gotchas
  for any future test author: an object type with **zero fields** is NOT unit — only an **absent**
  `returns.type` (e.g. `{"returns": {}}`) is; and any return-only/fail-only graph is independently rejected
  by the pre-existing `_assert_executable` ("must execute at least one operation") regardless of the return
  contract. **Active scope: step 3** (durable return store + atomic final settle).

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

## Step 2 — DONE (`returns.type` config + content_hash)

`ScriptRevision.return_type: Optional<ir.IrType>` (next to `arg_type`). `_return_type(cfg)` — parallel to
`_arg_type(cfg)` — reads config `"returns": {"type": <type>}`: absent `returns`, or `returns` present but
`returns.type` absent, ⇒ `Optional::None` (unit; the latter is the ONLY way to spell "explicit unit" — an
object type with zero fields is a distinct, non-unit value); a present `returns.type` is parsed + required
object via `ir.parse_object_type`, else `RunnerError`. `_return_type` ALSO requires the `returns` wrapper
itself be a JSON object with exactly its allowed key (`type`, or none for unit) — a non-object `returns` or
an unknown key inside it is `RunnerError`, never silently unit (review-flagged gap, fixed same step: a typo
like `{"returns":{"types":...}}` must not silently produce an unintended absent-return contract).
`_registry_build` computes `return_type` after `arg_type`, calls `ir.validate_return_contract(&graph,
&return_type)` (a rejection ⇒ `RunnerError` ⇒ `invalid_config`/`revision_unavailable`, never a dispatch),
and stores it on `ScriptRevision`. `_content_hash` takes `return_type` and appends
`_return_type_hash_suffix` — empty for unit (every pre-existing hash byte-identical),
`ir.canonical(return_type)` for non-unit. **Gate:** `just test` (runner) green — `ir_graph_test`/
`ir_exec_test` base+asan `exit=0`, 99/99 parser fixtures, full binary builds; `coordinator-singular`
integration green — 215/215 (`EXPECTED_CHECKS` bumped 208→215 for 7 new checks:
`returns_absent_hash_unchanged`, `returns_nonunit_changes_hash`, `returns_non_object_type_rejected`,
`returns_nonunit_implicit_fallthrough_rejected`, `returns_nonunit_explicit_return_builds`,
`returns_wrapper_non_object_rejected`, `returns_wrapper_unknown_key_rejected`). **Deferred
(unchanged from step 1):** the per-expression structural check that an explicit non-unit `return <expr>`
matches `return_type`'s declared fields — still terminal-shape only. **No parser un-gate, no
runner/storage** (steps 3–6 untouched), as scoped.

## Literal Next Action — 1b.0a step 3 (durable return store + atomic final settle)

Per `DESIGN.md`'s locked atomicity decision: the workflow's evaluated return is stored durably, **separate**
from per-op results, and written **atomically with completion** — the final settle transition writes, in ONE
fenced transaction, (a) the final op result (unchanged, `tb_mf_operation.result_json`), (b) the workflow
terminal return (new: a workflow-return store — schema/column not yet designed), and (c) `state=completed`.
Implies `sp_mf_operation_settle` (or its final-settle path) grows a `workflow_return_json` parameter,
accepted only when `is_final=1`. This step is schema/SP work (`microflows/db/schema`, `microflows/db/procs`)
plus the runner call site that invokes final settle — no design has been drafted yet for the store's shape
(new column on `tb_mf_workflow` vs. a new table); that is the first sub-decision to make before touching SQL.
Do not touch parser un-gating in this step either — un-gating `return` still waits on steps 3–6 together.

Then step 4 (runner finality probe passes `Completed(result)` into final settle instead of discarding it) →
5 (terminal replay from stored return, not graph re-derivation) → 6 (child-call result binding + the
deferred structural expr check) → un-gate `return`; then 1b.0 registry gate, 1b.1 runtime spine, and 1c per
the `DESIGN.md` checklists.
