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
- **Slice 1c** — compensation path: **DECIDED** — reverse-child/T1 is the single MVP mechanism (child
  `completed(4)→reversing(2)`, fenced, reopened by its own already-known `child_workflow_id`);
  compensating-workflow (a separate compensation-workflow identity) is explicitly out of MVP, so no author
  mode-selector is ever exposed. **Locked invariant:** a parent compensates the child call as **one
  checkpoint**; the child workflow recursively owns its own unwind and the parent never enumerates or invokes
  child-internal compensations directly.
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
budget is slice 2 (standalone). (3) *(SUPERSEDED — see the 1b.1 SP/schema plan's round-2 finding #3 below:
1c's T1 mechanism reopens the child by its own `child_workflow_id`, no separate compensation-workflow
identity is ever pinned, so `tb_mf_call` carries none of `comp_script_name`/`comp_plan_version`/
`comp_content_hash`.)* (4) The recursion ancestor set is **reconstructed by
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
- **`return <expr>` statement added to the parser and UN-GATED (step 6, DONE)** — see "Step 6 — DONE" below.
  `_parse_return` lowers via `_return_value_node` like any other terminal statement; the parser fixture
  corpus is 100/100 (the old gate fixture `err_return_unsupported` was removed and replaced with two
  positive `return`-lowering fixtures).
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
  **(3) durable return store + atomic final settle — DONE** (`tb_mf_workflow.workflow_return_json` +
  migration `0004`; `sp_mf_operation_settle`'s final-settle `UPDATE` writes it atomically with
  `state=completed`; two new entry `SIGNAL`s, not a structured outcome). **This also completed (4) and (5)**
  in the same pass — the runner finality probe now captures `Completed(result)` (previously discarded) and
  threads it into settle, and terminal replay (`sp_mf_workflow_inspect`) reads the stored
  `workflow_return_json` directly, never re-deriving from the graph — see "Step 3 — DONE" below for the
  full design + implementation record. `just test` (runner) green; SP-level `sp_operation_test.py` green
  156/156; coordinator-singular integration green 225/225.
  **The per-expression structural check — DONE, moved forward (not deferred to step 6 after all):** a
  post-implementation review flagged that `workflow_return` being externally visible/durable made the
  deferred check load-bearing sooner than planned. For a NON-unit `return_type`, `type_check_graph` now
  structurally validates every explicit `return <expr>`'s value against it (object-shape, not just
  terminal-reachability) — a scalar or wrong-shaped object is `invalid_config` at build. For UNIT, the
  value is intentionally NOT structurally constrained there (see "Step 3 — DONE" → "Post-implementation
  review round" below for why an earlier attempt at that broke pre-existing tests); "unit ⇒ `{}`" is
  instead enforced by runtime normalization.
  **Step 6 — DONE:** `return <expr>` is un-gated in the parser; `.mf` source (the user-facing surface)
  additionally gets a build-time rejection of an explicit non-null `return` under an undeclared/unit
  `returns.type` — see "Step 6 — DONE" below. Child-call result binding turned out to belong to **1b.0**
  (it needs a cross-script registry to resolve `child@plan_version` before it can even ask the child's
  return type), not step 6 — that + the rest of 1b.0 (input-contract validation, static cycle check) is
  the only piece left before 1b.1.

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
- **`return <expr>` is UN-GATED (1b.0a step 6, DONE)** — `_parse_return` lowers via `_return_value_node` like
  any other terminal statement; see "Step 6 — DONE" below for the full record (parser un-gating +
  `.mf`-source-only unit strictness + `coordinator-singular` integration coverage, 225/225).
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
  contract.
- **Slice 1b.0a step 3 (durable workflow return store + atomic final settle) is DONE + verified** —
  `tb_mf_workflow.workflow_return_json` (migration `0004_workflow_return.sql`) written atomically with
  completion by `sp_mf_operation_settle`'s final-settle branch, read back on replay by
  `sp_mf_workflow_inspect` (never re-derived from the graph). `Outcome::Completed`/`Outcome::AlreadyTerminal`
  gained `workflow_return` (the AUTHORITATIVE typed return) alongside the unchanged, compatibility-only
  `result` (last op's result). `just test` (runner) green; `sp_operation_test.py` green 156/156;
  `coordinator-singular` integration green 220/220 at the time (5 new checks, live+replay match for both
  unit and non-unit — non-unit tested then via the manual graph path, without un-gating `return` — plus a
  unit-normalization check). A post-implementation review also moved the per-expression **structural**
  return-value check forward into `type_check_graph` (non-unit only — see "Post-implementation review
  round" under "Step 3 — DONE" below for why unit is handled by runtime normalization instead), so it is
  DONE, not deferred. Full details + design record: "Step 3 — DONE" below.
- **Slice 1b.0a step 6 (un-gate `return <expr>`) is DONE + verified** — see "Step 6 — DONE" below.
  `coordinator-singular` integration green **225/225** (220 + 5 new `.mf`-source return-contract checks).
  **Active scope: 1b.0** (registry validation gate — call resolution, child input/return contract
  validation, static cycle check; design pending review below).

## Step 1 — DONE (IR return-contract validation)

`ir.validate_return_contract(g, return_type: Optional<IrType>)` (standalone; not yet wired into
`validate_graph`). Exact behavior: **unit** (`None`) accepts any terminal shape; a **non-unit** type must be
an **object** (else rejected — object-only); for a non-unit type **every successful sink `NReturn` must be an
explicit `return`** (value ≠ the implicit unit literal `null`) — an implicit unit fall-through on any
successful path is rejected (stricter than "no reachable return"); **`fail` (NFail) is exempt** (a
fail-only workflow is vacuously accepted). **Gate:** `ir_exec_test` base+asan `exit=0` (checks 170–177),
`ir_graph_test` base `exit=0`. **At the time this step landed**, the per-expression structural check (an
explicit non-unit `return <expr>` actually being object-shaped / matching `return_type`) was still deferred,
and `return` was still parse-gated — BOTH are since DONE: the structural check landed as a step-3 follow-up
(see "Post-implementation review round" under "Step 3 — DONE"), and `return` is un-gated (see "Step 6 —
DONE"). Object-only returns are now fully validated end-to-end.

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
`returns_wrapper_non_object_rejected`, `returns_wrapper_unknown_key_rejected`). **At the time this step
landed**, the per-expression structural check (unchanged from step 1's deferral) and the parser un-gate were
still pending — BOTH are since DONE (structural check: step-3 follow-up; parser un-gate: "Step 6 — DONE"
below). **No runner/storage** (steps 3–5 landed separately; see those sections), as scoped.

## Step 3 — DONE (durable workflow return store + atomic final settle)

**Status: DESIGNED, SIGNED OFF, IMPLEMENTED, and VERIFIED.** This section is the design + implementation
record for step 3. Facts were verified against the current schema/procs/runner (not assumed) before
drafting. Review round 1 found 5 issues (non-final settle arg nullability, live/replay result-shape
inconsistency, wrong atomicity mechanism, unspecified `already_settled` return handling, and a
new-outcome-vs-`SIGNAL` style question) — all fixed inline below, each marked "REVISED"/"CORRECTED" at its
location so the fix is traceable. Review round 2 asked for two wording/framing clarifications (the
`workflow_return`/`result` authority hierarchy, and demoting the `AlreadySettled` test to a resilience/edge
case behind a PRIMARY acceptance test) — also applied inline below. **Implemented exactly per this design,
in the planned order** (schema+migration+fixture audit → SPs → host `Optional` arg + snapshot decode →
runner outcome shape + finality probe plumbing → tests/gates) — see "Implementation summary" at the end of
this section for the concrete result and gate status.

### Decision: a COLUMN on `tb_mf_workflow`, not a side table

**`workflow_return_json` (mediumtext, NULL, JSON-object-checked) on `tb_mf_workflow`.** Reasoning:

1. **Cardinality is 0-or-1 per workflow, forever** — a workflow completes (successfully) at most once; there
   is no "many returns per workflow" the way `tb_mf_operation` genuinely has many rows per workflow (one per
   `operation_seq`). A side table keyed 1:1 (or 1:0) by `workflow_id` is a nullable column with extra steps —
   it buys no cardinality benefit a real one-to-many table provides.
2. **Direct precedent already exists on this exact table**: `terminal_reason` (`varchar(190) NULL`, added by
   migration `0001_terminal_failed_state.sql`) is *already* "a nullable terminal-outcome-payload column on
   `tb_mf_workflow`, populated once at a specific terminal transition, read directly by `workflow_id` during
   inspect/replay." `terminal_reason` is the FAILURE-terminal payload (states 5/6/7); `workflow_return_json`
   is the exact parallel for the SUCCESS-terminal payload (state 4/completed) — same shape, same lifecycle,
   opposite branch. The two are mutually exclusive by construction (a row has one or the other, never both).
3. **Atomicity is free, not engineered — CORRECTED mechanism (the RPC layer does NOT default to
   autocommit; an earlier draft of this section was wrong about that)**: `sp_mf_operation_settle`'s
   `arg_is_final = 1` branch already does ONE `UPDATE tb_mf_workflow SET continuation=…, state=4,
   current_disposition=1, … WHERE workflow_id=…` (verified: `microflows/db/procs/sp_mf_operation_settle.sql:196-213`)
   under the row lock taken earlier in the same proc call (`SELECT … FOR UPDATE`, lines 88-95).
   `sp_mf_operation_settle` runs inside the host-owned manual transaction for the stored-procedure call:
   `_call_sp_doc` (`host.drift:2122-2129`) executes `conn.call(...)`, drains the result via
   `_read_result_doc`, then `_finish_stmt_and_commit` (`host.drift:2058-2071`) calls `rpc.commit(conn)`
   explicitly. The connection is in manual-transaction mode (`mariadb-rpc` defaults `autocommit=false`,
   confirmed in `packages/mariadb-rpc/src/lib.drift:336,501`; nothing commits until that explicit
   `rpc.commit()` call) — so everything the proc body writes during the one `CALL` (the operation UPDATE,
   the workflow UPDATE, the event/checkpoint INSERTs) sits in one open transaction until that single commit
   ACKs. Adding `workflow_return_json = arg_workflow_return_json` to the SAME final `UPDATE tb_mf_workflow …
   state=4 …` statement keeps the final op result, the workflow return, and the completed state in that
   SAME fenced transaction and the SAME explicit commit boundary — zero new lock, zero new transaction
   machinery, zero new commit call. (Note for the "no second write" invariant: pool reset/reuse is NOT a
   durability boundary — a reset rolls back any still-open work; durability only starts at the explicit
   `rpc.commit()` ack. This is also exactly why the already-settled lost-ack retry behavior below has to be
   specified explicitly rather than left implicit — see "Runner call-site plan.") A side table would need
   this exact same one-`UPDATE`-becomes-two-statements change (an `INSERT`/`UPSERT` into the side table
   alongside the existing `UPDATE`) — still one proc call under the same one commit, so still atomic, but
   with an extra row/index and no offsetting benefit given point 1.
4. **The read side costs zero MARGINAL round trips with a column** (revised — see "Replay/read path plan"
   below for why the SECOND round trip, `host.operation_result` → `sp_mf_operation_result`, is being KEPT,
   not eliminated): `sp_mf_workflow_inspect` (the FIRST call, already made by `_inspect_report`) already
   SELECTs `tb_mf_workflow` by `workflow_id` and already returns `continuation`/`terminal_reason` this exact
   way. Adding `workflow_return_json` to that SAME existing `SELECT`/`JSON_OBJECT(...)` gets the new field
   for free, riding along on a call that already happens — no new proc, no new query. A side table CANNOT
   get this for free — it would need its own lookup (a genuine extra round trip, or a JOIN the inspect proc
   doesn't do today), strictly worse than a column here regardless of what the `result` vs `workflow_return`
   field-contract question below resolves to.
5. **Forward-compatible with the eventual call-binding consumer**: DESIGN.md's `call` model pins
   `operation_id = child_workflow_id` — i.e., the parent's future `call_inspect` (1b.0/1b.1) will already be
   looking up the CHILD by its `workflow_id`. Co-locating the return on `tb_mf_workflow` means that lookup
   reads the child's return in the SAME row/query it already needs for state/continuation, instead of a join
   to a separate table.
6. **Fits the established migration shape exactly**: every migration to date
   (`0001_terminal_failed_state.sql`, `0002_reconcile_budget.sql`, `0003_pending_redispatch.sql`) is
   `ALTER TABLE … ADD COLUMN` (+ a backfill `UPDATE` in 0001's case) — never a new table. A column is the
   path of least surprise for whoever reviews/applies migration `0004`.

Nothing here is a one-way door: if a future need (retention/archival independent of `tb_mf_workflow`,
multi-value history, etc.) ever appears, the column can be migrated OUT to a side table later. No such need
is stated today, so building it now would be speculative.

### Schema / migration shape

**Base schema** (`microflows/db/schema/tb_mf_workflow.sql`) — canonical, fresh-install path (confirmed:
existing `reconcile_budget`/`pending_redispatch` columns from migrations 0002/0003 are ALREADY baked into
the base `tb_mf_workflow_operation.sql`, so base schema files are kept in sync with all applied migrations,
per this repo's convention):
- Add `workflow_return_json mediumtext NULL` immediately after `terminal_reason` (grouping the two
  terminal-outcome-payload columns together — failure payload, then success payload).
- Extend the `(state, …)` composite CHECK family (precedent: existing `(state, current_disposition)` line
  102 and `(state, execution_direction)` line 106 checks) with a NEW constraint mirroring
  `ck_mf_operation_status_result`'s bidirectional shape:
  ```sql
  CONSTRAINT `ck_mf_workflow_return` CHECK (
    (`state` = 4 AND `workflow_return_json` IS NOT NULL
       AND JSON_VALID(`workflow_return_json`) AND JSON_TYPE(`workflow_return_json`) = 'OBJECT')
    OR
    (`state` <> 4 AND `workflow_return_json` IS NULL)
  )
  ```
  i.e. completed ⟺ a JSON-object return is present; every other state ⟺ NULL. (No writer path ever sets
  this column outside the `is_final=1` branch that also sets `state=4` in the same statement, so this holds
  by construction — the CHECK is defense-in-depth/self-documentation, matching this schema's existing
  strictness style, not a constraint we depend on to prevent a real writer path.)

**Migration `0004_workflow_return.sql`** (next free number; same 3-step shape as `0001_terminal_failed_state.sql`
— MariaDB has no in-place `ALTER … MODIFY CHECK`, so add-column → backfill → add-constraint, in that order):
1. `ALTER TABLE tb_mf_workflow ADD COLUMN workflow_return_json mediumtext NULL AFTER terminal_reason;`
2. Backfill: `UPDATE tb_mf_workflow SET workflow_return_json = '{}' WHERE state = 4;` — every workflow
   completed under the CURRENT engine is, by construction, a unit return (there is no way today to produce a
   non-unit terminal — `return` is parse-gated and the graph-level `NReturn` op is unit-mapped on every
   pre-existing config), so `{}` is the only correct backfill value. Rows in states 5/6/7 (failure-terminal)
   or 1/2/3 (still in flight) are left `NULL`.
3. `ALTER TABLE tb_mf_workflow DROP CONSTRAINT <existing-state-check-name>, ADD CONSTRAINT ck_mf_workflow_return CHECK (…);`
   — add the new constraint from above (dropping/re-adding whichever existing composite check the new one
   extends, or adding it as a wholly separate named constraint alongside the existing ones — whichever
   keeps the diff smallest; needs one more look at the exact existing constraint SQL before finalizing, but
   is mechanical either way).

**Gate before this lands**: audit `microflows/db-tests/coordinator/scenarios/coordinator-fixtures/` for any
seeded row with `state = 4` — if one exists, its seed data must ALSO carry `workflow_return_json = '{}'`
(or a fixture-appropriate value) or the new CHECK constraint will reject the seed at scenario-apply time.
This must be checked and fixed as part of this same change, not discovered later as a broken fixture.

### SP change shape

**`sp_mf_operation_settle`** gains one parameter, placed next to the existing `arg_result_json`/
`arg_checkpoint_payload` group (full new signature — only the ADDED line is new):
```sql
CREATE PROCEDURE `sp_mf_operation_settle`(
	IN arg_workflow_id varbinary(16),
	IN arg_executor varbinary(16),
	IN arg_fencing_token bigint,
	IN arg_operation_seq int,
	IN arg_operation_id varbinary(16),
	IN arg_checkpoint_seq int,
	IN arg_result_json mediumtext,
	IN arg_checkpoint_payload mediumtext,
	IN arg_workflow_return_json mediumtext,        -- NEW: required (JSON object) iff arg_is_final=1, else NULL
	IN arg_new_continuation mediumtext,
	IN arg_event_ts datetime(6),
	IN arg_event_payload mediumtext,
	IN arg_is_final tinyint(1)
)
```
**Validation style — REVISED to match existing precedent, not a new structured-outcome pattern.** Every
existing entry-parameter check in this proc (`arg_result_json`, `arg_checkpoint_payload`,
`arg_new_continuation`, `arg_event_payload`, `arg_is_final`, …) is a `SIGNAL SQLSTATE '45000' SET
MESSAGE_TEXT = '...'` hard failure at proc ENTRY, before any row lookup (verified:
`sp_mf_operation_settle.sql:52-85`) — NOT a structured `JSON_OBJECT('outcome', …)` result. The structured-JSON
outcome style (`not_found`, `already_settled`, `fence_lost`, `plan_violation`, …) is reserved for
LOGIC-level outcomes discovered AFTER entry validation passes (the call was well-formed, but durable STATE
didn't allow it). `arg_workflow_return_json`'s validity is a pure function of two INPUT parameters
(itself + `arg_is_final`) — no DB state needed — so it belongs with the other SIGNAL-style entry checks,
not as a new outcome. Two new SIGNALs, placed immediately after the existing `arg_is_final` check (since
they read its value):
```sql
IF arg_is_final = 1 AND (arg_workflow_return_json IS NULL
    OR JSON_VALID(arg_workflow_return_json) = 0 OR JSON_TYPE(arg_workflow_return_json) <> 'OBJECT') THEN
	SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowReturnJsonInvalid';
END IF;
IF arg_is_final = 0 AND arg_workflow_return_json IS NOT NULL THEN
	SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'MfWorkflowReturnJsonUnexpected';
END IF;
```
These are "should never happen" guards against a RUNNER bug (the runner always constructs the call
correctly), exactly like the other 9 entry SIGNALs — never intentionally triggered in normal operation, and
they need NO new host-side decode/outcome handling: they surface exactly however the existing 9 entry
SIGNALs already surface today (whatever generic `HostException`/error classification `conn.call(...)`
already produces for a `SIGNAL`-raised proc failure) — this is a deliberate NEW validation-style departure
worth flagging (finding: existing entry validation uses `SIGNAL`, this is the first time a param's validity
depends on ANOTHER param's value) but it reuses the SAME mechanism, not a new one. **This also resolves the
nullability question**: since the check is `arg_workflow_return_json IS NULL` (true SQL NULL) when
`arg_is_final = 0`, the SQL parameter must be able to receive a genuine NULL — see the host-side `Optional`
plumbing below (`rpc.arg_null()` already exists in the RPC library for exactly this).
- The `arg_is_final = 1` branch's existing `UPDATE tb_mf_workflow SET continuation=…, state=4,
  current_disposition=1, … WHERE workflow_id=…` (lines 196-213) gains `workflow_return_json =
  arg_workflow_return_json` in the SAME statement. The `arg_is_final = 0` branch (lines 214-230) is
  untouched — it never writes this column.
- No change to the idempotency (`already_settled`) or fence-check ordering/logic, and no return-related
  change to the `OperationSettleOutcome::AlreadySettled` payload itself (still just the per-op
  `result_json`, unchanged). **How a lost-ack final-settle RETRY renders `workflow_return` is specified
  explicitly in "Runner call-site plan" below — not left implicit** (finding: `already_settled` today only
  carries the operation result; resolved by having the runner reuse its OWN locally-computed value rather
  than adding a payload to `AlreadySettled` or doing a follow-up read).

**`sp_mf_workflow_inspect`** — add `workflow_return_json` next to the existing `continuation`/
`terminal_reason` selection and to the output `JSON_OBJECT(...)` (verified current body:
`microflows/db/procs/sp_mf_workflow_inspect.sql`, the `SELECT … INTO` at the top and the final
`JSON_OBJECT(...)` at the bottom). Emit as `JSON_EXTRACT(v_return, '$')` when non-NULL, `NULL` (JSON null)
otherwise — matching how `continuation` is already emitted. No new stored procedure needed for the read
side; this is a small addition to an existing one.

### Host (`host.drift`) decode/API changes

- `operation_settle` trait signature (line ~571) gains `workflow_return_json: &Optional<String>` — **REVISED
  from an earlier draft that said "always a concrete `&String`," which directly contradicted the SP's
  `arg_is_final=0 ⟹ NULL` validation rule above** (an earlier internal inconsistency in this design, now
  fixed): `None` for `is_final=0` calls (and for the legacy single-op path is irrelevant since that path is
  always final), `Some(json)` for `is_final=1` calls. This is a genuinely new pattern for this file (no
  existing `operation_settle`-adjacent wrapper takes an `Optional`-typed RPC argument today), but the RPC
  library already supports it natively — `rpc.arg_null()` exists in `mariadb-rpc` (`packages/mariadb-rpc/src/lib.drift:378`)
  for exactly this, so no library change is needed, only a new small host-side helper (below).
- New helper `_json_object_or_null_arg(field: String, opt: &Optional<String>) -> rpc.RpcArg`: `None` →
  `rpc.arg_null()`; `Some(s)` → the same object-shape validation `_json_object_arg` already does (non-empty +
  `_is_json_object`), else `HostErrorKind::InvalidJson`. Mirrors `_json_object_arg`/`_meta_object_arg`
  (`host.drift:1992-2003`) in style; a new helper rather than overloading an existing one because the
  existing two never emit `NULL`.
- RPC arg builder (`_call_sp_doc` args list, ~line 1456-1470): one more
  `args.push(_json_object_or_null_arg("workflow_return_json", workflow_return_json));` in the position
  matching the new SP parameter order.
- `OperationSettleOutcome` — **NO new variant needed** (revised — an earlier draft proposed
  `InvalidReturnJson`, which was modeled on the wrong validation style; see "SP change shape" above: the new
  checks are `SIGNAL`-based entry validation, like the 9 that already exist, and need no new structured
  outcome/decode logic).
- `WorkflowSnapshot` (decoded from `sp_mf_workflow_inspect`, ~lines 1536-1551) gains
  `workflow_return_json: Optional<String>` (`None` when not yet completed), decoded the same way
  `continuation` is decoded today (`JSON_EXTRACT` → `_doc_object_text_req`/equivalent-with-null-handling).

### Runner call-site plan

**Scoping correction (verified against the actual function structure — `_run_forward` is ONE function/loop,
not two separate call sites as an earlier draft implied):** `final_result`/`last_op_id_hex`/`settled` are
declared ONCE before the replay/dispatch `while` loop (`runner.drift:1825-1846`) and mutated across
iterations; `is_final` is declared freshly INSIDE the loop body per iteration (`runner.drift:2009`, since
it's a per-op-dispatch flag). `workflow_return` must follow the FIRST pattern — declared once before the
loop (e.g. `var workflow_return: String = "{}";`, alongside `final_result`), because it is SET at the
finality-probe site (below) on one iteration and READ later — either later in the SAME iteration (to build
the `operation_settle` argument) or, once completion is reached, when constructing the final `Outcome`.

1. **Finality probe** (`runner.drift:2010-2013`) — currently `ir.StepOutcome::Completed(_) => { is_final =
   true; }`, discarding the payload. Changes to capture it: `ir.StepOutcome::Completed(result) => { is_final
   = true; workflow_return = _workflow_return_for_settle(&result); }`. This is the ONLY place the value can
   be captured in time to hand it to `operation_settle` — the probe deliberately re-runs `ir.advance` with a
   HYPOTHETICAL `settled` array (prior settled ops + this op's about-to-be-persisted result) BEFORE the
   actual settle call, specifically so `is_final` (and now the return value) are known before that call.
2. **The settle call** (`runner.drift:2019-2020`) gains the new argument, built from `is_final` and
   `workflow_return`: `Some(_dup(&workflow_return))` when `is_final`, else `None` (never a concrete `"{}"`
   string here — see the `Optional` revision above; the SP requires true SQL NULL for the non-final case).
3. **Constructing `Outcome::Completed`** (`runner.drift:1859`, the SAME loop's `ir.StepOutcome::Completed(result)`
   arm reached on a LATER iteration once every op is durably settled) — this is where `Outcome::Completed`
   itself gains a `workflow_return` field, populated from the SAME `workflow_return` local set in step 1
   above. **Why no separate fetch/re-derivation is needed here**: `_run_forward` is only ever entered for a
   workflow in `state=1` (forward) — a workflow already `state=4` (completed) is terminal and is routed
   through `_inspect_report`/`_report_terminal` instead, never through `_run_forward`. Since the SAME atomic
   `UPDATE` that sets `workflow_return_json` ALSO flips `state` to 4 (in one commit — see the atomicity
   section above), it is impossible for `_run_forward` to observe "final op durably settled" without ALSO
   having just gone through the live probe+settle path for that same op IN THIS CALL (a workflow can never be
   claimed into `_run_forward` with its last op already settled but `state` still 1 — those two facts always
   land together). So the `Completed(result)` arm at line 1859 is always reached having JUST set
   `workflow_return` on an earlier iteration of this SAME call — never a stale/default value.
4. **Lost-ack final-settle retry (`AlreadySettled`) — resolved explicitly, not left implicit, but this is a
   RESILIENCE/EDGE case, not the primary replay path** (finding: the `AlreadySettled` payload today carries
   only the operation result, nothing return-related). Note on when this arm actually fires: after a
   successful final settle, the workflow is already durably `state=4`; the NEXT normal drive attempt for that
   workflow routes through `_inspect_report`/`_report_terminal` (terminal inspect, since the workflow is now
   terminal), NOT back through `_run_forward` — so `AlreadySettled` on the FINAL op specifically only occurs
   in the narrow window of a retried call that is STILL racing/uncertain about whether its own prior attempt
   landed (e.g. a lost-ack retry within the same drive attempt), not as the general "resume a completed
   workflow" path (that's terminal inspect, covered under "Replay/read path plan"). With that scoped: the
   probe (step 1) runs UNCONDITIONALLY before the settle call, regardless of whether that call eventually
   returns `Settled` or `AlreadySettled` — so `workflow_return` is ALREADY populated by the time either arm
   is reached, on THIS attempt. Both arms (`Settled(res)` and `AlreadySettled(res)`) need NO special
   return-handling of their own: the runner renders `workflow_return` from its own locally-computed value in
   both cases. This is sound because `ir.advance` is a deterministic pure function of the durable settled-op
   set (the "restart-deterministic" property this IR already relies on elsewhere, e.g. `ir_exec_test`'s
   same-args-same-settled-same-outcome check) — a retried attempt recomputes the IDENTICAL value the
   original successful attempt computed and durably stored, so there is no drift to reconcile and no need to
   read the value back from storage on this path.

**Unit normalization at the capture site**: the graph's implicit terminal is the literal `EConst("null")`
(verified: `flat_to_graph` and the parser both produce this for fall-through/`return null`). Per the LOCKED
"unit normalization" rule, the EXTERNAL representation of unit is `{}`, never `null`. The runner — not the
IR — performs this mapping at the capture site (`_workflow_return_for_settle(result: &String) -> String {
if _seq(result, &"null") { return "{}"; } return _dup(result); }`), because the IR's job (via
`validate_return_contract`) is already scoped to REJECTING bad shapes, not re-encoding good ones, and this
mapping is a runner/host-boundary (external-representation) concern, not an IR-internal one. This single
helper handles both today's always-unit workflows AND, once un-gated, an explicit non-unit `return <expr>`
(which never produces the literal `"null"`, so the helper is a no-op for it).

**Legacy single-op path** (`runner.drift:932-933`, no graph/IR at all) — always unit AND always final by
construction (no `NReturn` is possible without a graph, and a single-op plan always settles with
`is_final=1`); its `operation_settle` call passes `Some("{}".to_string())` (or equivalent) — always Some,
never None, since this path is always final. Zero new computation needed at this call site.

Neither the settle call nor the `Outcome::Completed` construction needs to know the workflow's declared
`return_type` — the value threaded through is always "whatever the finality probe already computed," which
is correct for unit AND non-unit workflows alike given the IR-level contract already guarantees (per
`validate_return_contract`) that every completed non-unit workflow's `Completed(result)` is a real explicit
return, never the unit sentinel.

### Replay/read path plan

**One explicit contract (REVISED — an earlier draft was internally inconsistent here: it proposed dropping
the `operation_result` lookup on replay while ALSO claiming `result` keeps meaning "last op's result" — if
the lookup is dropped, `result` can no longer be honestly populated on replay. Resolved by picking the
option that keeps steps 3-5 boring: `result` is UNCHANGED, everywhere, and `workflow_return` is ADDED
everywhere.)**

**The two fields are NOT peers — one is authoritative, one is legacy compatibility (clarified per review;
an earlier draft left them looking like two equal-weight "results," which invites confusion about which one
a caller should actually trust):**
- **`workflow_return` is the workflow's CONTRACT result** — the authoritative, declared-typed-return value
  (unit `{}` today, since `return` is still gated; the real explicit `return <expr>` value once un-gated).
  This is what a caller/parent (and, eventually, `call_inspect`'s child-result binding) should read.
- **`result` is retained for compatibility/debugging only** — it keeps its legacy "last settled operation's
  result" meaning verbatim, unchanged by step 3, and is NOT the workflow's answer in the general case (a
  multi-op workflow's last op result and its declared return can differ once `return` un-gates). It stays
  because existing callers/tooling already depend on it and removing/repurposing it is out of scope here.

- `result` (on BOTH `Outcome::Completed` and `Outcome::AlreadyTerminal`) keeps its EXACT current meaning
  ("the last settled operation's result") and its EXACT current population path, live and on replay,
  unchanged by step 3. Reason to keep rather than repurpose now: `return` stays parse-gated through step 6,
  so during steps 3-5 `workflow_return` is trivially `{}` for every expressible workflow (nothing can produce
  a non-unit terminal yet outside a hand-authored test graph) — replacing a currently-informative field with
  one that's always `{}` for every real workflow today would be a regression, not an improvement, and
  changing an existing field's MEANING is a breaking change for any existing caller/tooling regardless.
- `Outcome::Completed` and `Outcome::AlreadyTerminal` BOTH gain a NEW field, `workflow_return: String`
  (additive, safe). Live path: populated from the `workflow_return` local already threaded through in
  "Runner call-site plan" above — zero extra work, it's already computed by the time `Outcome::Completed` is
  built. Replay path: `_report_terminal`'s `STATE_COMPLETED` branch (`runner.drift:2565-2582`) reads
  `snap.workflow_return_json` off the `WorkflowSnapshot` already fetched by `_inspect_report`'s
  `host.inspect_workflow(...)` call — riding along on a call that already happens, zero new round trips.
- `host.operation_result(workflow_id, result_seq)` → `sp_mf_operation_result` — **KEPT, not removed**, on the
  replay path (an earlier draft proposed eliminating this call; that's now retracted per the contract
  above — `result` still needs it). The existing 3-way `OperationResultOutcome` match at this call site is
  unchanged. No change to `sp_mf_operation_result` itself or any other caller.
- CLI JSON render (`runner.drift:150-151`) gains `"workflow_return":...` on both the `completed` and
  `already_terminal` shapes, alongside the existing `"result":...` key (unchanged) — e.g.
  `{"workflow":"completed","operation_id":...,"result":...,"workflow_return":...}`.
- Whether `result` should eventually be deprecated/repurposed once `return` fully un-gates (step 6) and
  downstream tooling has had a chance to migrate to `workflow_return` is an explicit FUTURE decision, not a
  step-3 one — flagged here so it isn't silently forgotten once workflows can produce answers that actually
  differ from their last op's raw result.

### Migration/backfill behavior for existing completed workflows

Covered above (migration step 2): every pre-existing `state = 4` row gets `workflow_return_json = '{}'` in
one `UPDATE ... WHERE state = 4`, matching the shape of `0001_terminal_failed_state.sql`'s own
`terminal_reason` backfill (which similarly derived a new column's value for pre-existing terminal rows from
existing durable facts — there, from `tb_mf_workflow_event`; here, from the invariant that no non-unit
return was ever possible before this feature, so the answer is always the unit constant). No workflow needs
re-running or re-settling; this is a pure at-rest data migration, applied once, before the new CHECK
constraint is added (ordering matters: backfill must happen between the `ADD COLUMN` and the `ADD
CONSTRAINT`, exactly like 0001).

### Tests / gates

- **Fixture audit (blocking)**: confirm/fix any `coordinator-fixtures` seed row with `state = 4` to also
  carry `workflow_return_json = '{}'`, or the new CHECK rejects the seed scenario at apply time.
- **Reject invalid final settle**: `is_final=1` with NULL/malformed `workflow_return_json` → the new
  `MfWorkflowReturnJsonInvalid` `SIGNAL` fires (a hard proc failure, matching how the other 9 existing entry
  `SIGNAL`s behave — NOT a structured JSON outcome), workflow row UNCHANGED (still forward/pre-completion) —
  no partial write.
- **Reject smuggled non-final return**: `is_final=0` with a non-NULL `workflow_return_json` → the new
  `MfWorkflowReturnJsonUnexpected` `SIGNAL` fires, no column written.
- **Atomicity**: verify `_call_sp_doc` → `_finish_stmt_and_commit` → `rpc.commit(conn)` is the SOLE commit
  point for this proc call (i.e. confirm no other commit happens mid-proc) — this is the mechanism, not
  `autocommit`, that makes the op-result write + workflow-return write + `state=4` write land together;
  reviewed as an architectural invariant (one `UPDATE`, one open transaction, one explicit commit), not
  something crash-injected in this harness.

**PRIMARY acceptance (this is the normal path — every other test below is either a component of this or an
edge case around it):**
- **Final settle stores the return atomically; terminal replay reads `workflow_return_json`; live and replay
  JSON match.** Concretely: drive a workflow to completion in one call → `Outcome::Completed` carries
  `workflow_return` (the authoritative contract result) and `result` (unchanged, last-op-result,
  compatibility-only). Then drive the SAME (now-terminal) workflow again — this goes through
  `_inspect_report`/`_report_terminal` (terminal inspect), NOT `_run_forward`, since the workflow is already
  `state=4` — and `Outcome::AlreadyTerminal` must report the IDENTICAL `workflow_return` (read back from
  `sp_mf_workflow_inspect`, not re-derived from the graph) and the IDENTICAL `result` (still via
  `sp_mf_operation_result`, unchanged). Both fields must match byte-for-byte between the live-completion view
  and the later terminal-replay view of the same completion. This is the test that actually exercises the
  full step-3 feature end to end; the others below cover specific components or edge cases of it.
- **Unit workflow** (today's default, any existing plan/graph config, no `returns.type`): completes with
  `workflow_return_json = '{}'`; both live `Completed` and replayed `AlreadyTerminal` report
  `workflow_return: "{}"`.
- **Non-unit workflow — testable NOW, without un-gating `return`**: exactly like step 2's tests, a manual
  `"graph"` config with an explicit non-null `NReturn` node (the graph-level `return` node is NOT
  parse-gated — only the `.mf` source-level `return` statement is) drives a real non-unit completion through
  this whole path end-to-end: settle stores the exact value, `sp_mf_workflow_inspect` returns it verbatim,
  replay reports it without re-deriving from the graph (provable the same way the existing "terminal replay
  needs no registry/config rebuild" property is proven — call inspect/replay with the config file deleted or
  swapped for something incompatible, and confirm the reported return is unaffected).

**SECONDARY / resilience case (edge case around the primary path, not itself the normal replay flow):**
- **Idempotent re-settle (`AlreadySettled`) renders the SAME `workflow_return`**: this covers the narrow
  lost-ack-retry window WITHIN a single drive attempt, not the general "resume a completed workflow" case
  (that's the primary acceptance test above, which goes through terminal inspect instead). Settle the final
  op once, then retry the SAME settle call before the workflow has been re-claimed (simulating a lost ack) —
  the runner must report the IDENTICAL `workflow_return` on both the original `Settled` response and the
  retried `AlreadySettled` response, proving the "reuse the locally-computed value, no second write, no
  drift" reasoning in "Runner call-site plan" actually holds. Keep this test, but it is a resilience check on
  a rare race, not the thing that proves the feature works.
- **Backfill**: apply migration `0004` against a pre-migration snapshot carrying `state=4` rows lacking the
  column → post-migration, those rows read back `workflow_return_json = '{}'` and the new CHECK holds for
  every row in the table.
- **No content_hash change**: step 3 is durable-storage plumbing only; unlike step 2, nothing here touches
  `content_hash`/plan identity — worth an explicit regression check (existing content_hash fixtures/tests
  from step 2 must stay byte-identical) so the two steps aren't confused.
- **Full regression**: `just test` (runner unit: `ir_graph_test`/`ir_exec_test` base+asan, 99/99 parser
  fixtures, full binary build) and `just test` (coordinator-singular integration) both green, same as every
  prior step's gate.

### Open questions — resolved during implementation

1. The `(state, workflow_return_json)` constraint (`ck_mf_workflow_state_return`) was added as a **wholly
   new, standalone** CHECK — `terminal_reason` (the closest existing precedent) never had its own CHECK
   constraint to extend, so there was nothing to fold into; it sits alongside the existing
   `ck_mf_workflow_state_disposition`/`ck_mf_workflow_state_direction` pair, same bidirectional-implication
   style as `ck_mf_operation_status_result`.

(Two other open questions from the design draft resolved without code impact: settle needed no new
structured outcome — it uses `SIGNAL` like existing entry validation, so there's no new
`OperationSettleOutcome` variant; and `sp_mf_operation_result` is NOT partly dead code — it stays exactly as
used today, since `result`/`sp_mf_operation_result` are explicitly KEPT per the "Replay/read path plan"
contract, not removed.)

### Implementation summary

Implemented in the planned order, each step verified before moving to the next:

1. **Schema + migration + fixture audit** — `microflows/db/schema/tb_mf_workflow.sql` gained
   `workflow_return_json mediumtext NULL` (after `terminal_reason`) + `ck_mf_workflow_state_return`;
   `microflows/db/migrations/0004_workflow_return.sql` (add column → backfill `state=4` rows with `'{}'` →
   add constraint, same 3-step shape as `0001_terminal_failed_state.sql`). Fixture audit found exactly the
   two `state=4` rows the design predicted (`coordinator-fixtures`' `tb_mf_workflow.data.csv` rows `...02`/
   `...03`, i.e. `WF_COMPLETED_UNSETTLED`/`WF_COMPLETED_NO_OP`) — backfilled to `{}` in the fixture CSV
   itself (a header + all-rows rewrite, done with a small Python/csv transform, not by hand) so the scenario
   still applies cleanly under the new CHECK.
2. **SPs** — `sp_mf_operation_settle` gained `arg_workflow_return_json` (two new entry `SIGNAL`s —
   `MfWorkflowReturnJsonInvalid`/`MfWorkflowReturnJsonUnexpected` — matching the existing 9, not a new
   structured-outcome style) and writes it in the SAME final-settle `UPDATE` that sets `state=4`.
   `sp_mf_workflow_inspect` gained `workflow_return_json` in its existing `SELECT`/`JSON_OBJECT` — no new
   proc. `microflows/db-tests/sp_operation_test.py`'s 9 positional `sp_mf_operation_settle` call sites were
   updated for the new parameter position; 8 new checks added (156/156 total, up from 148).
3. **Host** — `operation_settle`'s trait + `HostImpl` signature gained `workflow_return_json:
   &Optional<String>` (a first for this file — no prior RPC arg was `Optional`-typed); new
   `_json_object_or_null_arg` helper (`rpc.arg_null()` for `None`, object-shape validation for `Some`, never
   substituting `{}` for `None` — that would violate the SP's NULL-when-non-final requirement).
   `WorkflowSnapshot` gained `workflow_return_json: Optional<String>`, decoded via a new
   `_doc_object_text_opt` (parallel to `_doc_object_text_req`, but `None` on JSON null since `{}` is itself a
   meaningful value distinct from "absent" — unlike `terminal_reason`'s `""`-sentinel trick).
4. **Runner outcome shape + finality-probe plumbing** — `Outcome::Completed`/`Outcome::AlreadyTerminal` each
   gained a `workflow_return: String` field (additive; `result` unchanged in meaning and population
   everywhere — the two-field hierarchy from review round 2 held exactly as specified). The finality probe
   (`ir.StepOutcome::Completed(result)`, previously `Completed(_)`) now captures the value, maps the
   internal unit sentinel (`"null"`) to the external `"{}"`, and threads it through as `Some(...)`/`None`
   keyed on `is_final` — a new `workflow_return` local declared alongside `final_result`/`last_op_id_hex`
   (same lifetime: set on one loop iteration, read on a later one). `_report_terminal`'s `STATE_COMPLETED`
   branch reads `snap.workflow_return_json` directly (no new round trip — rides along on the
   already-happening `inspect_workflow` call) while KEEPING the existing `operation_result` call for
   `result`, per the locked one-explicit-contract design. The legacy single-op path (no graph/IR) always
   passes `Some("{}")`. Both `driftc` entry points (`microflows.runner::main` and `::service_main`) compile
   clean with these changes.
5. **Tests/gates** — new coverage at every layer: `sp_operation_test.py` (SIGNAL rejection ×3, atomic
   storage read-back, inspect round-trip on both a terminal and an active workflow, AlreadySettled
   no-second-write) — 156/156; `coordinator-singular` integration (`workflow_return_unit_live_completed` /
   `_replay_matches_live`, `workflow_return_nonunit_live_completed` / `_replay_matches_live` — the non-unit
   case exercised NOW via the manual "graph" config path, without waiting for `return` to un-gate, reusing
   the exact technique from step 2's tests) — `EXPECTED_CHECKS` 215→219, **219/219 green**. Full regression
   (`just test` for the runner: `ir_graph_test`/`ir_exec_test` base+asan, 99/99 parser fixtures, full binary
   build) stayed green throughout — no step-2 behavior (content_hash, `returns.type` validation) was
   touched or regressed.

**Net effect**: `workflow_return` is now the authoritative typed workflow/function return, durably stored
atomically with completion and read back on replay without graph re-derivation; `result` remains an
unchanged compatibility/debugging surface. `return` itself is still parse-gated (unaffected by this step).

### Post-implementation review round — structural return-expression check moved forward (partially)

A follow-up review flagged: since `workflow_return` is now externally visible/durable, shipping it while the
"deferred structural check" (an explicit `return <expr>`'s VALUE actually matching `return_type`'s shape —
step 1 only ever checked terminal-shape/reachability) was still deferred to step 6 meant a manual graph
could (a) declare unit and explicitly return a non-`{}` value, contradicting "unit ⇒ `{}`", or (b) declare a
non-unit object type and return a scalar/wrong-shaped object, discovered only as a live SQL `SIGNAL` at
settle instead of `invalid_config` at build.

**(b) is now fixed at build time**: `type_check_graph` (`ir.drift`) gained a `return_type` parameter and,
for a DECLARED (non-unit) return type, structurally checks every `NReturn` value against it via
`_check_value_is` — the SAME machinery an operation's input is checked against its contract with (literal-
by-value via `validate`, or inferred-type assignability). A scalar or wrong-shaped object is now rejected at
build (`invalid_config`), never persisted. Pinned at `ir_exec_test.drift` checks 178/180/181 (positive
exact-match, scalar rejected, extra-field rejected).

**(a) — an earlier fix attempt was WRONG and was reverted.** The first attempt also rejected an explicit
non-`{}` return under `return_type=None` (unit) inside `type_check_graph`. This broke a PRE-EXISTING,
unrelated test (`ir_exec_test.drift` check 140, `gty1` — an `NIf`-condition type-check fixture that returns
a plain int under no declared return type at all) — `type_check_graph` is `ir.drift`'s general-purpose
graph type-checker, shared by a large body of tests that author arbitrary manual graphs under
`return_type=None` for reasons having nothing to do with the 1b.0a return-type feature; constraining all of
them to a `{}`-shaped return would have rejected that whole surface. **Reverted, and "unit ⇒ `{}`" is instead
enforced by RUNTIME NORMALIZATION**: `_run_forward` (`runner.drift`) now takes `return_type` and forces
`workflow_return = "{}"` unconditionally whenever it is `None`, regardless of what the graph's `Completed`
value actually is — the graph's return value is simply never consulted for a unit script. Pinned at
`ir_exec_test.drift` check 179 (revised to assert `type_check_graph` does NOT reject this — normalization
happens elsewhere) and, end-to-end, at the `coordinator-singular` integration check
`workflow_return_unit_normalizes_explicit_graph_return` (a unit script whose graph explicitly returns
`{"id":99}` still reports/persists `{}`).

**Verification**: `ir_exec_test` standalone binary exit 0 (all checks including 178-182); full `just test`
(runner) green — `ir_graph_test`/`ir_exec_test` base+asan, 99/99 parser fixtures, full binary build (both
`::main` and `::service_main` entry points); `coordinator-singular` integration green, `EXPECTED_CHECKS`
219→220, **220/220**.

Callers of `type_check_graph` updated for the new parameter: `_registry_build` (passes the real
`return_type`), `_pc_typecheck` (the `--parse-check`/`--lower-source` source-language driver — always unit,
since `.mf` `returns`/`return` are still parse-gated), and the `ir_exec_test.drift`/`parser_test.drift` test
helpers (`_tc_ok` always unit; new `_tc_rt`/`_tc_rt_rejects`/`_tc_rt_accepts` for the return-type-aware
cases).

This narrows **step 6** to: un-gating `return <expr>` in the parser + the `.mf`-source-facing test surface —
see "Step 6 — DONE" below. Child-call result binding turned out to belong to **1b.0** after all (DESIGN.md's
own text: "Parent binding (lands in 1b.0)" — it needs a cross-script registry to resolve `child@plan_version`
before it can even ask what the child's return type IS, which is exactly 1b.0's job, not step 6's).

## Step 6 — DONE (un-gate `return <expr>`; `.mf`-source unit strictness)

**Scope, as confirmed with the user before implementation:** `returns.type` stays manifest/config-sourced —
no new `.mf`-level `returns { }` syntax (DESIGN.md's own deferred choice: "a `.mf` `returns` block can come
later"). Concretely: (1) un-gate `.mf` `return <expr>`; (2) parse-check fixtures prove syntax/lowering only;
(3) contract validation (does the returned VALUE satisfy a declared `returns.type`) is tested through the
manifest/config path, where `returns.type` actually exists; (4) a `.mf` workflow with no `returns.type` is
unit/back-compat, but — UNLIKE the manual "graph" config path, which keeps step 3's compatibility
normalization — `.mf` source is the user-facing surface: an explicit non-null `return` there without a
declared return contract must be a build/config error, not silently normalized away.

**Un-gating.** `parser.drift`'s `_parse_return` no longer throws `unsupported-in-release`; it calls the
existing general-purpose `_parse_value_expr` and constructs a real `Stmt(kind = KReturn(value))`, exactly
mirroring `_parse_fail`'s shape. The `KReturn` → `_return_value_node` lowering arm and the `_stmt_falls_through`
arm were already written (kept dormant since slice-1a) and needed zero changes. `_all_plain_ops` (used to
decide flat-"plan" vs control-flow-"graph" lowering) already treats any non-`KOp` statement as
graph-forcing, so an explicit `return <expr>` anywhere in the source automatically lowers to a real
`NReturn(value=<expr>)` inside a `"graph"` config — never the flat-plan's auto-generated implicit-unit
terminal — with no changes needed to that dispatch logic either.

**The `.mf`-source-only unit strictness (the new, non-dormant piece).** A hand-authored manual `"graph"` JSON
config and a `.mf`-source-lowered config converge to the IDENTICAL `"graph"` shape by the time either reaches
`_registry_build` — there is no structural way to tell them apart post-lowering. Since the user's requirement
was asymmetric (manual graph keeps step 3's `{}`-normalization; `.mf` source gets build-time rejection
instead), a PROVENANCE marker was needed. `parser.drift`'s `_merge` now stamps `"mf_source": true` onto every
config it emits (content-hash-neutral — `_content_hash` never reads arbitrary top-level `cfg` keys, only
`_graph_bindings`-scoped per-operation resolution). `ir.drift` gained `pub fn validate_source_unit_return(g)`:
for a `return_type = None` config, walks every successful-path `NReturn` sink and rejects if its value is not
the unit sentinel (`_is_unit_return_value` — the literal `null`, whether from implicit fall-through or an
authored `return null`) — deliberately NOT reusing `_check_value_is`/`empty_object_type()` here, since the
user's rule is stricter than "must be shaped like `{}`": even an explicit `return {}` under an undeclared
contract is rejected, not just a non-`{}` value. `runner.drift`'s `_registry_build` calls this ONLY when
`_config_bool(cfg, "mf_source", false)` is true — so `validate_return_contract`/`type_check_graph` (called
UNCONDITIONALLY for every config) stay exactly as permissive as before, and the general-purpose manual-graph
testing surface (`ir_exec_test.drift`, `coordinator-singular`'s `graph_cfg()` helper,
`workflow_return_unit_normalizes_explicit_graph_return`) is untouched — it keeps the step-3 normalization
compatibility behavior on purpose.

**Fixtures.** `err_return_unsupported.mf`/`.expected` (whose entire point was proving the now-gone gate) was
removed; two new `check/` fixtures (`return_bare`, `return_result_projection`) prove parse+lower+structural
validate+type-check (always unit at this layer — `--parse-check` has no `returns.type` concept) for a plain
object return and a named-operation result-projection return. The two existing `lower/` fixtures
(`lower_base_strip`, `lower_overlay`) were reblessed to include the new `mf_source: true` key (no other
diff) — `100/100` parser fixtures green (was 99: -1 removed, +2 added).

**`coordinator-singular` integration ("C5l" section)** exercises the manifest/config path end-to-end
(`lower_source`/`lower_source_stderr` extended with a `returns=` override, building a per-call base config
instead of the shared one) — 5 new checks: non-unit `returns.type` + matching return completes with the
correct `workflow_return`; non-unit + wrong-shaped return rejected at build (the step-3-follow-up structural
check, proven via `.mf` source, not just manual graph); non-unit + an if/else where only one branch
explicitly returns rejected at build (`validate_return_contract`'s pre-existing rule, likewise proven via
`.mf` source); no declared `returns.type` + an explicit non-null return rejected at build (the NEW check);
no declared `returns.type` + an explicit `return const null` still accepted. `EXPECTED_CHECKS` 220→225.

**Bug caught by the FIRST integration run, fixed before landing**: `validate_source_unit_return`'s initial
version took only `g: &IrGraph` and unconditionally rejected any explicit non-null return — it never checked
whether `return_type` was actually `None` before rejecting, so a `.mf` script that DID declare a non-unit
`returns.type` (with a correctly-matching explicit return) was wrongly rejected too
(`mf_return_nonunit_matching_shape_completes` failed: lowering itself exited 3, "workflow declares no return
type (unit)" — even though a `returns.type` WAS declared). Fixed by giving the function a
`return_type: &Optional<IrType>` parameter and early-returning `None()` when it is `Some(_)` (the non-unit
case already has its own, separate structural check in `type_check_graph` — this function is unit-only).
Re-verified clean on the next run.

**Verification**: full `just test` (runner) green — `ir_graph_test`/`ir_exec_test` base+asan (ir.drift gained
a function but no existing logic changed), 100/100 parser fixtures, full binary build; `coordinator-singular`
integration green, `EXPECTED_CHECKS` 220→225, **225/225** (after the fix above; the first attempt was 224/225).

Then **1b.0 = build-time registry validation gate** (no DB, no runtime): resolve `call <child>@<plan_version>`
against a manifest-scoped registry by exact plan identity; validate the call input against the child's
declared arg/input contract; bind the child `return` type so `result <call_id>.path` validates downstream
(unit ⇒ every path rejected); reject static call cycles at build. See the design section below (pending
review) before implementation starts.

## 1b.0 — build-time registry validation gate (DONE)

**Scope, confirmed with the user before implementation.** Two rounds of clarification landed on:
1. Layered build validation for a `call <child>@<plan_version>` node, in order: (a) resolve `child@plan_version`;
   (b) validate the call's input against the child's declared arg/input type; (c) bind the child's declared
   return type so downstream `result <call_id>.path` is meaningful; (d) reject static call cycles; (e)
   `compensation`/`fan`/`on failed` stay rejected until 1c/slice 3 (already true, unaffected).
2. **Executability stays gated through 1b.0.** Even after (a)–(d) all pass for every call in every script, a
   graph with a reachable call is STILL rejected — not runnable until 1b.1 (the runtime spine). This is a
   DELIBERATE decision: `ir.advance` has zero dispatch semantics for `NCallWorkflow` today (confirmed: nothing
   in `runner.drift` even references `NCallWorkflow`), and building a stub/fault execution path now would be a
   half-feature blurring the 1b.0/1b.1 slice boundary. 1b.0's value is a REFINED failure surface: a
   malformed call fails for its PRECISE reason (unresolved target / bad input shape / bad result-path
   projection / cycle) instead of today's one blanket message; a FULLY VALID call graph still fails, but with
   an explicit, distinct "runtime lands in 1b.1" message — proving the validation itself ran and passed.
   Tests must show BOTH kinds of rejection.

**Current baseline this replaces**: `_assert_executable` (ir.drift, inside the op-depth walk) unconditionally
rejects any reachable `NCallWorkflow` today with one message ("workflow call is not runnable until
composition slice 1b"), regardless of well-formedness — this is slice 1a's frontier-only gate, and it runs
for EVERY `_registry_build` caller (single-script CLI/service dispatch included), not just manifest mode.

**Where cross-script resolution has to live.** A `call`'s target is ANOTHER script in the manifest — resolving
it, checking its input/return contract, and detecting cycles all need visibility across every script in the
manifest at once. Today `_registry_build(cfg)` builds exactly ONE script's `ScriptRevision` (arg_type,
return_type, graph, content_hash, plan_length) with zero cross-script visibility, and `_load_manifest`
computes each script's `ScriptRevision` via `_registry_build` but DISCARDS it into a leaner `ManifestScript`
(name/version/cfg/content_hash_hex/plan_length) — there is no existing "the whole manifest's resolved
scripts" structure to validate a call against. `_registry_resolve`/`_find_script`/`_find_script_nv` all
resolve "which ONE script THIS invocation should run," never "what does script X's call to script Y resolve
to" — confirmed zero existing cross-script call-resolution code in ir.drift or runner.drift.

**Proposed two-pass shape, scoped narrowly to avoid touching `ManifestScript`'s existing (dispatch-selection)
callers:**
- **Pass 1 (per-script, order-independent, inside `_load_manifest`'s existing loop):** parse/lower each
  script and compute its `ScriptRevision` as today, EXCEPT a new manifest-mode `_registry_build` variant
  defers `_assert_executable`'s call-rejection (a call may be structurally reachable; don't reject yet — pass
  2 needs every script's arg_type/return_type/graph to be known FIRST). Also collect, in a local (non-persisted,
  not bolted onto the public `ManifestScript` struct) array: `{name, version, arg_type, return_type, graph}`
  for every script — this is purely an ephemeral cross-check input, not new public API surface.
- **Pass 2 (manifest-wide, after all scripts are known, still inside `_load_manifest`):** for every script's
  graph, for every `NCallWorkflow(id, child, plan_version, input, ...)` node:
  - Resolve `(child, plan_version)` by EXACT match against the pass-1 array. Unresolved → `invalid_config`,
    "call target script/version not found in manifest: `<child>@<plan_version>`".
  - Validate `input` against the resolved target's `arg_type` — reuses `_check_value_is`/`_infer_expr_type`,
    the SAME machinery an operation's input is checked against `OpContract.input_type` with. Mismatch →
    `invalid_config` with the same style of message `type_check_graph` already uses for operation inputs.
  - For every `EResult(this_call_id, path)` reference anywhere in the CALLER's graph: if the resolved target's
    `return_type` is `None` (unit) → reject (ANY path access on a unit-returning call is invalid — "unit ⇒ all
    `result <call_id>.*` paths rejected", per DESIGN.md's own wording); if `Some(t)` → project `path` against
    `t` (reuses `_project_type`, the same projection `_infer_expr_type`'s `EResult` arm already uses for typed
    operation results) and reject on a shape mismatch.
  - Cycle check: build a graph whose NODES are the distinct `(name, version)` pairs present in the manifest and
    whose EDGES are each script's own call targets restricted to targets that ALSO exist in the manifest (a
    call to a target NOT in the manifest is already caught as "unresolved" above — no need to double-count it
    in the cycle graph). Adapt `_topo_order`'s existing Kahn's-algorithm cycle detection (today scoped to one
    graph's node ids) to this cross-script node set; a cycle (including a direct self-call at the same version)
    → `invalid_config`, "static call cycle detected" naming the path.
  - If (a)-(d) all pass for every call in every script → THEN, for each script that has at least one reachable
    call node, throw the distinct "runtime lands in 1b.1" rejection (replacing today's blanket message for
    THIS validated-but-still-non-executable case specifically). A script with NO reachable call is completely
    unaffected — same behavior as today.

**Single-script (`--config`, no manifest) mode is unaffected on purpose.** A lone `--config` invocation has no
sibling scripts to resolve `call` against; `_registry_build`'s EXISTING signature/behavior (today's blanket
call-rejection via `_assert_executable`) stays exactly as-is for every non-manifest caller (single CLI/service
dispatch, `ir_exec_test.drift`, `graph_cfg`/`plan_cfg`-based integration tests, etc.) — none of those
currently author `call` nodes, so this is a zero-risk, zero-diff path for the entire existing test surface.
The existing slice-1a parser fixtures (`call_single.mf` etc.) are similarly unaffected: `--parse-check`'s
`_pc_validate`/`_pc_typecheck` call `ir.validate_graph`/`ir.type_check_graph` directly and never invoke
`_assert_executable` at all, so they don't exercise this gate either way.

### Implementation summary

Implemented in the 6 gates given, verified after each: (1) resolve `call child@plan_version` against the
manifest registry; (2) validate call input against the child's declared arg type; (3) bind the child's
declared return type so `result <call_id>.path` type-checks downstream; (4) reject result access on a
unit-returning child; (5) static cycle detection across pinned call edges; (6) keep a reachable
`NCallWorkflow` non-runnable until 1b.1, replacing the blanket message with precise validation first, then a
distinct final gate.

**ir.drift** — new exported `pub struct CallTarget { name, version, arg_type, return_type }` (the resolved
callee's contract, built by `make_call_target(name, version, &arg_type, &return_type)` — deep-clones via the
existing private `_clone_type`, since a struct built in runner.drift needs its own owned copy) and `pub struct
CallEdge { node_id, child, plan_version }` + `pub fn call_edges(g) -> Array<CallEdge>` (the raw, unresolved
declarations — used for cross-script cycle detection and by the manifest-wide re-check).

`type_check_graph` gained a 5th parameter, `call_targets: &Optional<Array<CallTarget>>` — `None` (every
existing caller: `_pc_typecheck`, `ir_exec_test.drift`/`parser_test.drift` helpers, and even
`_registry_build`'s own per-script pass, see below) means exactly today's permissive behavior, unchanged
byte-for-byte; `Some(targets)` (ONLY the new manifest-wide re-check) makes `NCallWorkflow`'s input get
resolved + validated (same `_check_value_is` machinery an operation's input uses) and makes `EResult`
referencing a call resolve through `targets` instead of the operation-only `_op_result_type` — unit ⇒ a HARD
reject for any path (not `_op_result_type`'s "permissive Unknown" convention, which is reserved for an
UNTYPED operation and means something different), typed ⇒ project via the same `_project_type` an operation's
typed result already uses. Threading `call_targets` required updating `_check_value_is`/`_tc_loop`/`_tc_merge`
and every recursive `_infer_expr_type` call site (any expression position — object/array fields, loop/merge
bodies — could nest an `EResult` referencing a call), all mechanical.

`op_depth`/`nonfinal_operations`/their shared `_node_depths` core gained an `allow_calls: Bool` parameter —
`false` everywhere existing (preserves the blanket "workflow call is not runnable" rejection exactly);
`true` ONLY for manifest-mode's PER-SCRIPT build pass (`_registry_build`, called from `_load_manifest`'s
existing per-script loop) — that pass must compute `plan_length`/`arg_type`/`return_type` for a script with a
reachable call BEFORE the manifest-wide pass has run (a chicken-and-egg problem: cross-script validation
needs every sibling's contract, which requires every sibling to have already built). `_assert_executable`/
`_assert_reversible`/`_registry_build` all thread this through; every one of their other call sites (single
CLI/service dispatch, `--emit-content-hash`, `--lower-source`) passes `false`, unaffected.

**runner.drift** — `_load_manifest`'s per-script loop now ALSO collects `registries: Array<ScriptRegistry>`
(parallel to `scripts`, index-matched — kept local, never bolted onto `ManifestScript`'s public shape, so
`_find_script`/`_drive_manifest_request`'s dispatch-selection callers are untouched). After the loop, a NEW
`_validate_manifest_calls(&scripts, &registries)` runs: builds `call_targets` from every script's resolved
contract, RE-INVOKES `ir.type_check_graph` per script with the real targets (accepted as simpler and safer
than inventing a parallel surgical re-check — graphs are small, re-running the full pass costs nothing and
can't silently diverge from the real one), runs `_assert_no_call_cycles` (a flat `(from_index, to_index)`
edge-list Kahn's algorithm — NOT a nested `Array<Array<Int>>` adjacency list, which driftc rejected with
"cannot copy value of type 'Array'"; a script whose in-degree never reaches 0 is part of a cycle, which also
correctly catches a direct self-call), and ONLY THEN, for any script with `ir.call_edges(&graph).len > 0`,
throws the final distinct "static validation passed, but runtime dispatch is not implemented until
composition slice 1b.1" — never the old blanket message, and never reached at all for a script that failed
gates 1-5 first.

**Manifest test surface (new)** — `--manifest` mode's `_load_manifest` runs entirely DB-free (confirmed:
`_require_db` is only reached AFTER `_load_manifest` returns successfully), so EVERY 1b.0 rejection (today,
every manifest with a reachable call rejects — either for a precise gates-1-5 reason, or the final gate-6
"1b.1 pending" message) is testable with zero DB/stub setup. New `tests/fixtures/manifest/<name>/` (manifest
+ `.mf` scripts + a `run.json` naming the submitted script/arguments) + `tests/run_manifest_fixtures.py`
(mirrors `run_parser_fixtures.py`'s golden-diff pattern exactly: `{returncode, stderr_contains}` goldens),
wired into `justfile`'s `test` target. 6 fixtures, one per gate: `gates1_4_ok` (resolves, input validates,
result type-checks through a real projection — final message is the "1b.1 pending" gate, proving 1-4 AND 6
together); `gate1_unresolved`; `gate2_wrong_input`; `gate4_unit_reject`; `gate5_cycle_two`; `gate5_cycle_self`.

**Regression note (caught by manual testing before committing fixtures)**: a bare `call` with no other
operation gives `plan_length = 0`, tripping the PRE-EXISTING "a workflow must execute at least one operation"
rule — unrelated to 1b.0, but every hand-written test script needed a real op alongside its `call` to avoid
this red herring. Also: a parent script whose OWN explicit `return` is non-null under an undeclared
`returns.type` trips step 6's `validate_source_unit_return` BEFORE gate 4 is ever reached (since pass 1 runs
before the manifest-wide pass) — the `gate4_unit_reject` fixture isolates gate 4 by routing the call's result
through a `let` binding instead of the parent's own `return`, since `type_check_graph`'s `NLet` arm always
type-checks its bound value regardless of whether the binding is later used.

**Verification**: full `just test` (runner) green — `ir_graph_test`/`ir_exec_test` base+asan, 100/100 parser
fixtures, 6/6 manifest fixtures (new), full binary build (both `::main` and `::service_main` entry points).
No changes needed to `coordinator-singular` — 1b.0 introduces no new single-script-visible behavior (every
existing test there passes `call_targets = None`/`allow_calls = false` implicitly, unchanged).

### Post-implementation review round — two real gaps found and fixed

A follow-up review caught two issues in the first 1b.0 landing, both confirmed and fixed before this
was considered done:

**1. (High) A call did not count as an executable step even under `allow_calls=true`.** `_node_depths`'s
`inc` computation only checked `_is_operation`, never `_is_call` — so a workflow whose ONLY step is a `call`
got `plan_length = 0`, tripping the PRE-EXISTING (unrelated) "a workflow must execute at least one operation"
rule BEFORE 1b.0's real validation/gate-6 path was ever reached. The `gates1_4_ok` fixture had (unknowingly)
masked this by prefixing every call-containing script with a dummy `ping {}` op. Fixed: `inc = 1` for a call
too, but ONLY when `allow_calls` is true (DESIGN.md's own terminology — "a workflow call is a pending CALL
OPERATION" — treats it as occupying one forward step, matching the eventual 1b.1 runtime); `allow_calls=false`
makes this branch unreachable (the graph is already rejected earlier), so no existing single-script caller is
affected. Fixing this correctly REQUIRED removing the dummy `ping {}` from every fixture — with the call now
correctly contributing to depth, a `ping` BEFORE the call is genuinely non-final and needs a compensation
binding it doesn't have (a second, correct rejection this fix surfaced, not a regression) — every fixture's
parent script is now the call by itself (or the call followed by nothing), which is both simpler and the
actually-intended shape. Added a dedicated, minimal fixture, `gate6_call_only_executable_step`, whose parent
is a single bare `call` with no other operation at all, reaching the "1b.1 pending" message directly — proving
this fix, isolated from every other gate.

**2. (High/Medium) The "child returns a different type than the parent" case was never actually proven.**
`_deployment_for` copied ONE shared `deployment` object for every script, overriding only `script_name`/
`plan_version` — so `gates1_4_ok`'s single deployment-level `returns.type` applied IDENTICALLY to both child
and parent. If gate 3/4's binding had a bug that used the caller's OWN return_type instead of resolving the
callee's, this fixture could not have caught it (both types were the same value). The user separately
generalized this finding into a broader requirement: a manifest holds multiple typed-function workflows
(`name@version : ArgsType -> ResultType`), and BOTH sides of that signature must be per-workflow, not just
`returns.type` — a deployment-level value may exist as a fallback default, never as the only source.

Fixed for **returns**: each `scripts[]` manifest entry may now declare its own `"returns"` key, which
OVERRIDES the shared `deployment.returns` for that script only (`_deployment_for` gained a
`returns_override: &Optional<json.JsonNode>` parameter — skips copying `base.returns` when an override is
given, then sets the override instead; `None` behaves exactly as before). `gates1_4_ok` was rewritten with
GENUINELY different types: child declares `returns.type = {charge_id: int}`, parent declares
`returns.type = {receipt_id: int}` (deliberately different FIELD NAMES so a wrong-type bug is structurally
unable to pass) — parent's `return { receipt_id: result x.charge_id }` can only build if `result x.charge_id`
resolves against the CHILD's own type (parent's type has no `charge_id` field) AND the parent's own return
value is checked against the PARENT's own type — a sound cross-check by construction, no separate negative
fixture needed.

**Arguments were investigated and found to need NO code change** — per-workflow argument typing is ALREADY
structurally guaranteed, for a different reason than returns: `.mf` SOURCE (not config) declares
`argument_type` via its own `args { }` block, and `parser.lower`'s `_merge` UNCONDITIONALLY derives
`argument_type` from the source, discarding whatever the wrapping config/deployment/manifest-entry would
otherwise carry — this is why `returns.type` (which has no `.mf` syntax and stays config-sourced) needed a
new manifest-schema field while `argument_type` did not: a manifest-level "arguments" override would be
INERT for every real manifest script (100% `.mf`-sourced today) since `_merge` always overwrites it from the
source regardless. `gate2_wrong_input`'s child/parent already declare genuinely different `args {}` shapes
and already prove the call-input check resolves against the CALLEE's own declared type, not the caller's —
this was true before this review round too, just not framed as the proof it already was.

**Verification**: manifest fixtures re-verified 7/7 (all messages precise — no accidental fallback errors);
full `just test` (runner) and `coordinator-singular` integration re-run clean after these fixes.

### Post-implementation review round 2 — remove shared/inherited manifest signatures entirely

The user tightened the model further: a manifest holds multiple typed-function workflows, and each one's
`returns` contract must be declared EXPLICITLY on its OWN `scripts[]` entry — no optional fallback to
`deployment.returns` at all (the previous round made per-script `returns` possible; this round makes it
MANDATORY and removes the fallback path entirely). Rules: `scripts[].returns` required on every entry;
no inheritance/default across scripts; explicit unit spelled `"returns": {}`, never omitted.

**`_deployment_for` simplified**: its `returns_override` parameter (previously `&Optional<json.JsonNode>`,
falling back to `base.returns` when absent) is now `returns_value: &json.JsonNode` — always given, no
fallback branch left in the function at all. `base.returns` (the shared deployment's, if present) is now
UNCONDITIONALLY stripped and never consulted, matching "no fallback" literally.

**`_load_manifest`'s per-script loop** now REQUIRES the `returns` key on every `scripts[]` entry — its
absence throws `RunnerError` immediately ("script 'X' is missing required 'returns' declaration... use
\"returns\": {} for an explicit unit return"), before the script is even lowered. This is a hard manifest
LOADING failure, not a downstream type-check rejection — a manifest missing this on any one script never
gets past parsing that entry.

**Manifest schema doc comment** rewritten to state the requirement directly, and to note that
`argument_type` needs no analogous manifest-level field: `.mf` source's own `args { }` block already makes
it per-script and mandatory-by-source-shape (unaffected by this round — see the prior round's finding that a
manifest-level "arguments" override would be structurally inert, since `parser.lower`'s `_merge`
unconditionally derives `argument_type` from the source regardless of what a wrapping config carries).

**Fixtures**: every existing manifest fixture updated to declare `"returns": {}` (or a real type, for
`gates1_4_ok`) on EVERY script entry — none rely on omission or a deployment-level default anymore. Added a
NEW dedicated fixture, `gate_missing_returns_rejected` (a script entry that omits `returns` entirely),
proving the new mandatory-declaration rule is enforced with the precise message. 8 fixtures total.

**Verification**: manifest fixtures 8/8; full `just test` (runner) clean.

**Regression found and fixed by the FIRST `coordinator-singular` integration run after this change**: 6
PRE-EXISTING manifest-mode tests there (`manifest_submit_pins_and_runs`, `manifest_unknown_script_rejected`,
`manifest_resume_by_pin`, `manifest_relative_path_resolved`,
`manifest_resume_missing_revision_claimable_defers`, `manifest_resume_missing_revision_terminal_replays`)
construct their own manifest JSON inline via two shared helpers (`write_manifest`/`write_manifest_at`) plus
one hand-rolled site (`manifest_relative_path_resolved`) — none of their script entries declared `returns`,
since that requirement didn't exist when they were written. All 6 immediately failed with `invalid_manifest`
once `returns` became mandatory. Fixed by adding `"returns": {}` (unit — confirmed none of these tests' `.mf`
sources use an explicit `return`) directly inside the two helpers and the one inline site. The same sweep
also found and fixed two MORE pre-existing sites with the identical gap outside `coordinator-singular`:
`integration/coordinator-singular/perf.py`'s inline manifest (a "reserve" perf-test script) and the
committed example template `microflows/examples/manifest.json` (6 example workflows) — both confirmed unit
(no explicit `return` in any referenced `.mf`), both fixed the same way, and the template's own `_comment`
field updated to state the new per-script `returns` requirement for real users following it.

**Verification (re-run after the regression fix)**: `coordinator-singular` integration green, **225/225**,
0 FAILs — including all 6 previously-failing `manifest_*` checks now passing.

## 1b.1 — runtime spine for workflow calls (SP/SCHEMA PLAN, pending review — round 5)

Scope per the user's suggested slice: (1) durable call sidecar/schema; (2) SP/host surface
(`call_submit`/`call_inspect`/`child_terminal_notify`); (3) runner runtime for `NCallWorkflow`; (4) recursion
protection; (5) acceptance tests. This section covers ONLY (1)+(2) — the SP/schema plan the user explicitly
asked to review before any code, "because this is the first durable/runtime composition slice and we want
the transaction boundaries clean." (3)-(5) get their own design pass once this lands.

**Everything below is already fully specified in DESIGN.md's own 1b.1 checklist and the "5 findings" +
"second review round" sections of this file (lines 25-63) — this section is a CONFIRMATION/SYNTHESIS pass
against the CURRENT schema/SP code (verified fresh, not from memory), not a new design. Flagging any place
current code diverges from what DESIGN.md assumed.

**Round-1 review found 5 real gaps in the FIRST draft of this plan, all confirmed against the actual
reversal/inspect code (not assumed) and folded in below**: (1) HIGH — the existing reversal machinery
CANNOT actually no-op a call checkpoint (verified: `reverse_head`'s `Pending` outcome carries no
`call_kind`; the runner unconditionally calls `_compensation_for` on any pending checkpoint, and a call
checkpoint — which never has a compensation binding, since 1a rejects `compensation` at build — falls into
the EXISTING `no_compensation_binding` → indefinite-defer path, never actually reversing; separately,
`reverse_settle` hard-requires a persisted `reverse_invocation_id`, which a call checkpoint — never
dispatched — can never have). This needed a genuinely NEW mechanism, not just documentation; see "Reversing
a call checkpoint" below. (2) HIGH/MEDIUM — the first draft self-contradicted, calling `call_inspect`
"READ-ONLY" and then describing a write; fixed by making it a PURE read and moving hint-refresh elsewhere.
(3) MEDIUM — the sidecar's `comp_*` columns encoded a specific (compensating-workflow) shape; removed
entirely (see round-2 finding #3 below for the sharper reason: they were unneeded even under the model 1c
HAS already picked). (4) MEDIUM — `call_submit`'s idempotent-replay path needed to verify immutable-field
agreement, not just presence, mirroring `operation_conflict`'s existing pattern. (5) LOW — a missing
`CHECK(call_kind IN (1,2))`. A sequencing note on `child_terminal_notify`'s commit boundary (post-commit,
never nested in the child's own transaction) is folded in too.

**Round-2 review found 3 more gaps**: (1) HIGH — `call_submit` was missing the child's `tb_mf_workflow_args`
write entirely; a child created without it is not actually resumable through the normal planned-workflow
path (confirmed: `sp_mf_workflow_create_planned` writes this row in the same transaction as the workflow+plan
rows today, and `args_get`'s own doc comment states resume reads it as authoritative). Fixed — see
`sp_mf_call_submit` below, both the write and the idempotent-replay comparison. (2) MEDIUM — the "SPs" heading
still said "three new procedures, zero changes to existing ones", which had gone stale once round-1's fixes
landed (5 new SPs, plus `reverse_head` extended) — heading corrected. (3) MEDIUM — DESIGN.md itself (not just
this file) still described the sidecar carrying `comp_script_name`/`comp_plan_version`/`comp_content_hash`
(lines 288, 492-493) — stale relative to the round-1 #3 removal here. On inspection this is actually a
STRONGER case than "1c hasn't decided yet": DESIGN.md's own §Recursive-compensation-invariant (lines 140-177)
already resolved 1c to a SINGLE MVP mechanism — T1, reverse-child reopen (`completed(4)→reversing(2)` on the
CHILD'S OWN row, fenced) — and "compensating-workflow" (the only model that would ever need a SEPARATE
compensation-workflow identity to pin) is explicitly "not in MVP". So the sidecar never needed comp_*
columns even under the plan as originally conceived; DESIGN.md updated to match (see below).

**Round-3 review found 2 more gaps, one architectural**: (1) HIGH — `call_submit` still didn't write the
child's initial `tb_mf_workflow_event` (`kind='created'`) row, even after round-2 added the args write;
confirmed `sp_mf_workflow_create_planned` writes workflow + plan + args + this SAME created event as all
four parts of one transaction, and a child is a normal planned workflow instance, so it needs the identical
trail. Fixed. (2) HIGH, architectural — the recursion/depth guard was described as running "before any write
commits," which is the wrong boundary: the host commits unconditionally after reading the proc's result
document, with no way to distinguish a structured rejection from a success, so a guard that ran AFTER some
inserts had already been issued would let a rejected call's PARTIAL rows persist durably. Fixed by
restructuring `sp_mf_call_submit` into a strict validate-then-mutate shape — fence check → existing-row
idempotent check → recursion guard, ALL read-only and ALL completing before the first write statement of
any kind — matching the "validate first, mutate last, never interleaved" pattern every other SP in this
codebase already follows. See the rewritten `sp_mf_call_submit` section below.

**Round-4 review found 2 more gaps**: (1) HIGH — even after round-3's fixes, `call_submit`'s host signature
still didn't carry enough to actually create a valid planned child: verified against
`sp_mf_workflow_create_planned`'s own full parameter list, it's missing `plan_length` (needed for
`tb_mf_workflow_plan.plan_length` — nothing to write there without it), `continuation` (the child's own
fresh starting position — NOT the parent's), `next_attempt_at` (the child's initial scheduling field —
without it the child cannot become claimable at all), and its own `event_payload` for the child's `created`
event (distinct from the parent's `call_submitted` payload). Added `child_plan_length` /
`child_continuation` / `child_next_attempt_at` / `child_event_payload` to the host signature and threaded
them through the write-phase description. (2) LOW/MEDIUM — `tb_mf_call.child_workflow_id` had a UNIQUE key
but no structural FK to `tb_mf_workflow(workflow_id)`; added one, matching
`tb_mf_workflow_plan`/`tb_mf_workflow_args`'s own FK-to-`tb_mf_workflow` pattern (the child is created in
the SAME transaction, before the sidecar row, so "cannot point at a missing child" can be structural, not
just something the procedure happens to preserve).

### Schema

**`tb_mf_operation`** — add exactly one column, nothing else (keep the hot table narrow):
```sql
ALTER TABLE tb_mf_operation ADD COLUMN call_kind TINYINT NOT NULL DEFAULT 1 AFTER schema_version;
-- 1 = participant (existing/default, every current row), 2 = child_workflow.
ALTER TABLE tb_mf_operation ADD CONSTRAINT ck_mf_operation_call_kind CHECK (call_kind IN (1,2));
-- (round-1 finding #5) — matches this table's existing defensive-enum convention
-- (ck_mf_operation_status already does exactly this for `status`).
```
`operation_id`, `status`, `result_json` keep their EXACT existing meaning and `ck_mf_operation_status_result`
invariant, verified unchanged in the current schema — for `call_kind=2`: `operation_id = child_workflow_id`,
`operation_name = child_script_name`, `schema_version = CALL_OPERATION_SCHEMA_VERSION` (a new named
constant = 1, analogous to a participant's schema_version but for the call itself), `result_json` becomes
the child's `workflow_return_json` once settled (see "Settling a call op" below) — NOT a new shape, the
SAME JSON-object-or-null-with-status invariant already enforced.

**New sidecar `tb_mf_call`** (1:1 with a `call_kind=2` operation row; verified NO existing table of this
name):
```sql
CREATE TABLE tb_mf_call (
	workflow_id varbinary(16) NOT NULL,
	operation_seq int NOT NULL,
	child_workflow_id varbinary(16) NOT NULL,
	child_script_name varchar(128) NOT NULL,
	child_plan_version varchar(32) NOT NULL,
	child_content_hash varbinary(33) NOT NULL,
	-- Display hint only (§Liveness) — the CHILD workflow row is authoritative; call_inspect re-reads it.
	-- Refreshed ONLY by child_terminal_notify (terminal case) and the separate best-effort
	-- sp_mf_call_hint_refresh (non-terminal case) — NEVER by call_inspect itself (round-1 finding #2).
	child_status tinyint NOT NULL DEFAULT 1,  -- 1=pending 2=completed 3=failed 4=blocked
	first_requested_at datetime(6) NOT NULL,
	last_inspected_at datetime(6) NULL,
	PRIMARY KEY (workflow_id, operation_seq),
	UNIQUE KEY uq_mf_call_child (child_workflow_id),
	CONSTRAINT ck_mf_call_status CHECK (child_status IN (1,2,3,4)),
	CONSTRAINT fk_mf_call_operation FOREIGN KEY (workflow_id, operation_seq)
		REFERENCES tb_mf_operation (workflow_id, operation_seq),
	-- (round-4 finding #2) — the child is created in the SAME transaction as this row (by
	-- sp_mf_call_submit's own write phase, child row before sidecar row), so "cannot point at a
	-- missing child" can be structural, not just preserved by the procedure — matching
	-- tb_mf_workflow_plan's and tb_mf_workflow_args' own FK-to-tb_mf_workflow pattern.
	CONSTRAINT fk_mf_call_child FOREIGN KEY (child_workflow_id) REFERENCES tb_mf_workflow (workflow_id)
);
```
No `child_return_json` column (confirmed decision: notify is wake/hint-only, never a value-of-record) and
no liveness-budget columns (confirmed: stuck-child budget is slice 2, standalone). **Round-1 finding #3
(sharpened by round-2 finding #3):** also no `comp_script_name`/`comp_plan_version`/`comp_content_hash` —
these are not merely premature, they are UNNEEDED under the model 1c has actually decided on. DESIGN.md's
own §Recursive-compensation-invariant already resolves 1c to a single MVP mechanism: T1 (reverse-child
reopen, `completed(4)→reversing(2)` on the CHILD'S OWN workflow row, fenced, driven by the
`child_workflow_id` already in this very sidecar). Compensating-workflow — the only model that would ever
need a SEPARATE compensation-workflow identity pinned here — is explicitly out of MVP. So these 3 columns
were dead weight even under the plan as originally conceived, not just "speculative about an undecided
design"; 1b.1 has no compensation runtime at all (compensation stays build-rejected), and 1c's T1 mechanism
needs nothing more than the `child_workflow_id` this sidecar already carries.

**`tb_mf_workflow`** — add 4 ancestry columns, all NULL for a top-level (non-child) workflow:
```sql
ALTER TABLE tb_mf_workflow
	ADD COLUMN parent_workflow_id varbinary(16) NULL AFTER workflow_return_json,
	ADD COLUMN parent_node_id varchar(64) NULL AFTER parent_workflow_id,
	ADD COLUMN root_workflow_id varbinary(16) NULL AFTER parent_node_id,
	ADD COLUMN call_depth int NULL AFTER root_workflow_id,
	ADD CONSTRAINT ck_mf_workflow_ancestry CHECK (
		(parent_workflow_id IS NULL AND parent_node_id IS NULL AND root_workflow_id IS NULL AND call_depth IS NULL)
		OR (parent_workflow_id IS NOT NULL AND parent_node_id IS NOT NULL AND root_workflow_id IS NOT NULL AND call_depth IS NOT NULL AND call_depth >= 1)
	);
```
Confirmed: `plan_version`/`content_hash` already live on `tb_mf_workflow_plan` (PK `workflow_id`) — the
recursion-guard ancestor key needs NO new column beyond these 4; the walk joins `tb_mf_workflow_plan` per
ancestor as it goes.

**Migration**: next free number is `0005_workflow_call.sql` (`0001`-`0004` exist; `0004_workflow_return.sql`
is the cleanest 3-step ADD-COLUMN → BACKFILL → ADD-CONSTRAINT template — this migration needs the SAME
shape for the ancestry-columns CHECK, but NO backfill (every existing row is top-level: all 4 new columns
NULL satisfies the new constraint's first branch with zero data changes) plus a plain ADD-COLUMN + CHECK for
`tb_mf_operation.call_kind` (round-1 finding #5) and a plain CREATE TABLE for `tb_mf_call` (a new table needs
no migration at all for a fresh install, but the migration file still creates it for an already-deployed
schema, per every prior migration's own convention).

### SPs — five new procedures + one existing SP extended (round-2 finding #2: the prior heading understated
this — "zero changes to existing ones" was wrong, `sp_mf_checkpoint_reverse_head`'s output shape changes)

New: `sp_mf_call_submit`, `sp_mf_call_inspect`, `sp_mf_call_hint_refresh`, `sp_mf_child_terminal_notify`,
`sp_mf_checkpoint_reverse_noop`. Extended: `sp_mf_checkpoint_reverse_head`'s `Pending` outcome (+ the host
`ReverseHeadOutcome::Pending` variant) gains `call_kind`, per "Reversing a call checkpoint" below. Settling/
failing a call op FORWARD still reuses `sp_mf_operation_settle`/`operation_fail`/`begin_reversal` completely
UNCHANGED (that specific claim was accurate and stays true) — it is only the reversal-side + inspect/notify
surface that is new/changed.

**`sp_mf_call_submit`** — sibling of `sp_mf_operation_request`. **Round-3 finding #2 (HIGH) restructures this
SP's phasing entirely: strict validate-then-mutate, matching every other SP in this codebase, no exceptions.**
The earlier description said the recursion guard runs "before any write commits" — which is the WRONG
boundary. The host's `_finish_stmt_and_commit` calls `rpc.commit()` unconditionally after reading the proc's
result document, regardless of what the `outcome` string says; it has no way to know a structured
`call_rejected` outcome means "please roll back instead." So if the guard ran AFTER some inserts had already
been issued (op row, child workflow row, ...) and then returned a structured rejection via `SELECT` +
`LEAVE proc` (not a `SIGNAL`), the host would still commit those partial rows — a definite-failure outcome
would durably persist a half-created child. The only correct fix is architectural: **every check that can
possibly reject must run to completion, using ONLY reads, before the FIRST write statement of any kind is
issued** — exactly the shape `sp_mf_operation_settle`/`sp_mf_workflow_create_planned`/
`sp_mf_checkpoint_reverse_settle` already use (a sequence of SELECT-based structured-outcome checks, each
ending in `LEAVE proc` on failure, and only once every one of them has passed does the procedure reach its
single, uninterruptible write phase — never checks and writes interleaved).

Concretely, in order:
1. **Fence check** (read-only, same lease/owner/token pattern every other SP uses) → `fence_lost` on failure.
2. **Existing-row check** (read-only): does `(workflow_id, operation_seq)` already have an op row? If yes,
   this is a replay — compare the EXISTING row's + sidecar's + args row's immutable fields against what's
   being resubmitted (see "Idempotent replay" below) and `LEAVE proc` with either `already_submitted` or
   `call_conflict`. **No recursion guard re-walk on replay** — it already passed once, and nothing is being
   freshly created.
3. **Recursion guard** (read-only, ONLY reached on a genuinely fresh submit, never on replay): walk
   `parent_workflow_id` links from the PARENT upward (bounded by `parent's call_depth`, so at most
   `max_call_depth` hops), joining `tb_mf_workflow_plan` at each ancestor for its `(script_name @
   tb_mf_workflow, plan_version, content_hash @ tb_mf_workflow_plan)` triple; if the CHILD's own resolved
   plan identity (already known to the runner from 1b.0's build-time resolution, passed as an SP arg)
   matches ANY ancestor's triple → reject `call_cycle`; if `call_depth > max_call_depth` (config-supplied,
   default 16) → reject `max_call_depth_exceeded`. Both rejections are a structured outcome (JSON `{outcome:
   'call_rejected', reason: 'call_cycle'|'max_call_depth_exceeded'}`) — NOT a SIGNAL (matches 1b.0's finding
   #5: entry-validation-shaped failures are SIGNALs, this is a LOGIC-LEVEL/data-dependent rejection, same
   category as `plan_violation`/`fence_lost`) — and, critically, is still reached with **zero writes issued
   so far**, so `LEAVE proc` here needs no rollback: there is nothing to roll back. The runner turns this
   into a normal call failure (§4 `failed`) — same reversal path as any other definite forward rejection, no
   new failure surface.
4. **Only now, with every possible rejection already ruled out, the single write phase**: insert the
   `tb_mf_operation` row (`call_kind=2`, `schema_version=CALL_OPERATION_SCHEMA_VERSION`, `status=1`); insert
   the child `tb_mf_workflow` row under `child_workflow_id` with ancestry
   (`parent_workflow_id`/`parent_node_id`/`root_workflow_id`/`call_depth = parent's call_depth + 1`, or 1 if
   parent is top-level), `continuation = arg_child_continuation` (its OWN fresh starting position, not the
   parent's), and `next_attempt_at = arg_child_next_attempt_at`; insert the child `tb_mf_workflow_plan` row
   using `arg_child_plan_length` (round-4 finding #1 — verified against `sp_mf_workflow_create_planned`'s own
   full parameter list: `plan_length`/`continuation`/`next_attempt_at` are all REQUIRED inputs there, and
   `sp_mf_call_submit` needs the identical three to populate the identical two rows — without them there is
   no value to write into `tb_mf_workflow_plan.plan_length`, no starting position for the child's first
   forward step, and no way to make the child claimable at all); insert the child's `tb_mf_workflow_args` row
   (round-2 finding #1 — the call's `input`, canonicalized by the runner exactly as `create_planned`
   canonicalizes instance args, becomes the child's canonical args — confirmed necessary via `args_get`'s own
   doc comment: resume reads this table as authoritative, never CLI/submission input; a child without it
   would not be resumable/drivable after any lease-expiry/crash recovery); insert the child's initial
   `tb_mf_workflow_event` row, **kind='created'**, using `arg_child_event_payload` (round-3 finding #1,
   round-4 finding #1 clarifies the payload — confirmed via `sp_mf_workflow_create_planned`'s own body: it
   writes workflow + plan + args + this SAME `created` event, all four, in one transaction; a child is a
   normal planned workflow instance and needs the identical trail, not a subset of it, with its OWN event
   payload — NOT the parent's `call_submitted` payload, a distinct audit document); insert the `tb_mf_call`
   sidecar row; advance the PARENT's continuation; append the PARENT's own `call_submitted` event (using
   `arg_event_payload`, the separate parent-side payload). All of this is one `rpc.commit()` boundary
   (mirroring every other SP's "everything in one open transaction, one explicit commit" pattern) — but by
   construction, nothing between step 4's first write and its last can produce a structured rejection
   outcome; the only way this phase doesn't complete is a genuine DB-level failure (a different class from a
   validated business rejection), which is not this design's concern.

**Idempotent replay (round-1 finding #4 — must verify agreement, not just presence; round-2 finding #1 adds
the args row to what's compared)**: on the existing-row check (step 2 above) finding a prior op row, the SP
must verify the EXISTING row's immutable identity fields actually MATCH what's being resubmitted, not just
assume a match because the row exists: `operation_id` (mirrors `sp_mf_operation_settle`'s existing
`operation_conflict` check — "the supplied operation_id MUST match the stored one"), the sidecar's
`child_script_name`/`child_plan_version`/`child_content_hash` (these COULD legitimately differ across
attempts if e.g. the manifest was redeployed with a different `plan_version` between the original submit
and a retry), `input_hash`, `call_kind`, and the child's `tb_mf_workflow_args.args_canonical`, compared
byte-for-byte — mirroring `sp_mf_workflow_create_planned`'s own existing conflict check exactly
(`v_args_missing = 1 OR NOT (v_args <=> arg_args)` → `workflow_conflict`, confirmed verbatim in that SP's
body). On full agreement → `already_submitted` (idempotent, no new child spawned, no duplicate
recursion-guard walk, no duplicate writes of any kind). On ANY mismatch → a NEW structured outcome
`call_conflict` (mirroring `operation_conflict`'s existing shape/severity exactly — a real inconsistency,
never silently treated as "already submitted").

**`sp_mf_call_inspect`** — **round-1 finding #2 fix: PURE read, zero writes, no exceptions.** The first draft
self-contradicted (called it "READ-ONLY" then described a hint-refresh write). Adopting the reviewer's
stated preference exactly: this SP matches `sp_mf_workflow_inspect`'s existing shape byte-for-byte in kind
(no fence check, no write, no commit-relevant side effect, period). Given `(workflow_id, operation_seq)`,
joins `tb_mf_call` → the CHILD's `tb_mf_workflow` row (by `child_workflow_id`) and returns the child's
AUTHORITATIVE `state`/`workflow_return_json`/`terminal_reason`, plus `child_workflow_id` + the sidecar's
OWN (possibly-stale) `child_status` hint for the operator-inspect surface — it reads the hint, it does not
write it.

**`sp_mf_call_hint_refresh`** (NEW, small, split out of the old call_inspect design per finding #2) —
explicitly best-effort, matching `child_status`'s own "display hint, never value-of-record" nature (per
DESIGN.md's §Liveness): `UPDATE tb_mf_call SET child_status = ?, last_inspected_at = ? WHERE workflow_id = ?
AND operation_seq = ?`, guarding `last_inspected_at` from moving backward (mirrors `next_attempt_at`'s own
"never move backward" discipline in `child_terminal_notify` below). No fence check (a hint write, not a
correctness-affecting one). The runner may call this opportunistically after any `call_inspect` poll that
observes a NON-terminal state worth refreshing the hint for (in particular `blocked`, since
`child_terminal_notify` is terminal-only and would never otherwise update the hint for a merely-blocked
child) — its failure or staleness has zero correctness impact, by construction.

**`sp_mf_child_terminal_notify`** — WRITE-ONLY to two things: `tb_mf_call.child_status`
(the terminal hint) and the PARENT `tb_mf_workflow.next_attempt_at` (pulled due, monotonic — never moved
backward). Explicitly does NOT touch `tb_mf_operation`, does NOT touch checkpoints, does NOT touch the
parent's `status`/`result_json`/`state`/`lease`. Confirmed this matches DESIGN.md's explicit "wake +
status-hint ONLY" framing exactly — this SP is allowed to be lossy/racy/duplicate-safe without any settle
consequence, since `call_inspect` (called by the runner's own poll, which is always eventually scheduled
regardless of notify) is the actual settle trigger. **Sequencing note (raised alongside the 5 findings):**
this SP must be invoked as a SEPARATE call AFTER the child's own terminal-settle transaction has ALREADY
committed — never nested inside that same transaction. The child's own settle/reversal commit locks only
the CHILD's own `tb_mf_workflow` row; reaching into the PARENT's `tb_mf_call`/`tb_mf_workflow` rows in that
SAME transaction would lock two DIFFERENT workflows' rows in one transaction, an unproven cross-workflow
lock-ordering risk this system has never needed before (every existing publication fences + commits exactly
one workflow's rows per transaction). Since `call_inspect`/poll is the correctness floor regardless, notify
is safe to be strictly post-commit and best-effort — a missed, duplicated, or delayed notify changes
nothing except how soon the parent wakes up.

**Settling a call op FORWARD reuses `sp_mf_operation_settle`/`operation_fail`/`begin_reversal` UNCHANGED —
zero new SP code for this.** Confirmed via the current `sp_mf_operation_settle.sql` body: it is entirely
generic over `operation_name`/`operation_id`/`result_json` already (it never branches on any call-specific
concept), and it already copies `operation_name` into the checkpoint row (so a call op's checkpoint
`operation_name` = the child's script name automatically, becoming "the compensation envelope's
`forward.operation`" per DESIGN.md's finding — ready for 1c with no changes needed now). The runner, on
finding (via `call_inspect`) that the child is `completed`, calls the EXISTING host `operation_settle` with
`result_json` = the child's `workflow_return_json` — the call op's "remote result" IS the child's typed
return, verbatim, no transformation. On finding the child `failed`, the runner calls the EXISTING
`begin_reversal` (or `operation_fail` → the existing blocked_resolution path, whichever the child's specific
failure shape maps to) exactly as it would for a real participant's definite rejection. This is the concrete
payoff of the "`operation_id = child_workflow_id`, `schema_version`/`status`/`result_json` keep their
existing meaning" decision — it means settling/failing a call op FORWARD adds ZERO new lines to the busiest,
most safety-critical existing SP.

**Reversing a call checkpoint (round-1 finding #1 — HIGH, a genuinely NEW mechanism, not just documentation)**:
verified this is a real gap, not a documentation omission. Once a call op is settled forward (checkpointed
via the unchanged `sp_mf_operation_settle` path above), a LATER parent step failing puts the parent into
reversal — and the EXISTING reversal machinery cannot currently no-op that checkpoint:
- `sp_mf_checkpoint_reverse_head`'s `Pending` outcome carries only `(seq, operation_name, payload)` — no
  `call_kind` — so the runner's `_run_reversal` cannot distinguish a call checkpoint from a participant one
  BEFORE it acts.
- `_run_reversal` unconditionally calls `_compensation_for(cfg, operation_name)` on any `Pending` checkpoint;
  a call checkpoint (no compensation binding — 1a rejects `compensation` at build) hits the EXISTING `None`
  arm, which durably defers with reason `no_compensation_binding` and loops forever — it does NOT no-op, it
  never makes progress, indefinitely.
- `sp_mf_checkpoint_reverse_settle` hard-requires a persisted, matching `reverse_invocation_id` before it
  will transition `reversal_state` 1→2 (`not_requested` otherwise) — and a call checkpoint's
  `reverse_invocation_id` can never be set, because nothing is ever dispatched for it (`reverse_request` is
  only called immediately before a real compensation dispatch). The existing 3-step
  `reverse_request`→dispatch→`reverse_settle` flow is structurally incapable of ever marking a
  never-dispatched checkpoint reversed.

**Fix — two small, targeted additions:**
1. Extend `sp_mf_checkpoint_reverse_head`'s `Pending` outcome (and the `ReverseHeadOutcome::Pending` host
   variant) to ALSO return `call_kind` — a single-column join to `tb_mf_operation` by `(workflow_id, seq =
   operation_seq)`, giving the runner the information it needs BEFORE it ever calls `_compensation_for`.
2. NEW SP `sp_mf_checkpoint_reverse_noop` — fenced (same lease/owner/token check as `reverse_settle`),
   takes `(workflow_id, executor, fencing_token, seq, event_ts, event_payload)` — deliberately NO
   `reverse_invocation_id`/`reverse_operation_name`/`reverse_schema_version`/`reverse_input_*` parameters,
   since none of that ever applies. Verifies (a) the checkpoint is the current reverse-order top (mirrors
   `reverse_settle`'s existing `out_of_order` check), (b) `reversal_state = 1` (active) — idempotent
   `already_reversed` on retry, matching `reverse_settle`'s own idempotency shape, (c) defensively, that the
   checkpoint's operation is ACTUALLY `call_kind = child_workflow` (reject `not_call_checkpoint` otherwise —
   this SP must never be reachable for a participant checkpoint). On success: the SAME `reversal_state = 1
   → 2` transition `reverse_settle` performs, `reversed_at = arg_event_ts`, an audit event appended (e.g.
   `call_checkpoint_noop_reversed`) — but WITHOUT ever touching the `reverse_invocation_id`/`reverse_*`
   columns, which stay NULL throughout (satisfying `ck_mf_checkpoint_reverse_binding`'s existing all-NULL
   branch — this is a legitimate "never dispatched, still reversed" case that constraint already allows for).
3. Runner's `_run_reversal`, on `Pending(seq, operation_name, payload, call_kind)`: if `call_kind =
   child_workflow`, call the new `checkpoint_reverse_noop` host method DIRECTLY (skip `_compensation_for`
   entirely) and loop back to `reverse_head` to continue descending — exactly mirroring how a successful
   `reverse_settle` today continues the same loop. If `call_kind = participant` (existing default), the
   EXISTING `_compensation_for` → dispatch → `reverse_settle` flow is completely unchanged. (This is item-3
   runner-runtime work, listed here because it's the direct consequence of the schema/SP change and needs
   to land in the same slice — the schema addition is meaningless without it.)

### Host API (new, mirroring the confirmed `_call_sp_doc`/`_finish_stmt_and_commit`/`rpc.commit()` pattern
`operation_settle`/`operation_request` already use — same one-`rpc.commit()`-per-call shape, no new
transaction primitive needed):
- `call_submit(workflow_id, fencing_token, operation_seq, operation_id, child_workflow_id, child_script_name,
  child_plan_version, child_content_hash, child_plan_length, child_continuation, child_next_attempt_at,
  child_event_payload, input_json, parent ancestry fields, event_ts, event_payload) -> CallSubmitOutcome`
  (`Submitted`/`AlreadySubmitted`/`CallConflict`/`CallRejected(reason)`/`FenceLost`/... mirroring
  `OperationRequestOutcome`'s existing variant shape, plus `CallConflict` for round-1 finding #4).
  **Round-4 finding #1 (HIGH) — the signature was missing the fields needed to actually create a valid
  planned child.** Verified against `sp_mf_workflow_create_planned`'s own full parameter list: besides
  identity (`workflow_id`/`script_name`/`plan_version`/`content_hash`, already present) and `args` (already
  added in round-2), it ALSO takes `arg_plan_length`, `arg_continuation`, `arg_next_attempt_at`, and its OWN
  `arg_event_payload` (the created event's payload — a separate document from whatever audit payload the
  CALLING context uses) — without these, the SP has no value to populate `tb_mf_workflow_plan.plan_length`
  with, no starting `continuation` for the child's first forward step, and no `next_attempt_at` to make the
  child claimable at all. Added: `child_plan_length` (the child plan's own length, already known to the
  runner from 1b.0's build-time resolution of the child, same as `child_content_hash`/`child_plan_version`);
  `child_continuation` (the child's starting continuation — its fresh initial position, JSON object, NOT
  reused from the parent's own continuation); `child_next_attempt_at` (when the child becomes claimable —
  ordinarily the same clock reading the runner already took for this dispatch, i.e. "now"); `child_event_payload`
  (the child's OWN `created` event payload, distinct from `event_payload`, which remains the PARENT's
  `call_submitted` audit payload). `event_ts` stays a SINGLE shared clock reading used for both the parent's
  and the child's timestamps in this one command — mirrors `sp_mf_workflow_create_planned`'s own single
  `arg_event_ts` covering workflow/plan/args `created_at` AND the created event's `event_ts` together; no
  separate `child_event_ts` is needed.
  **Implementation note (entry validation):** "same shape as `create_planned`" implies, but should say
  explicitly so it isn't missed in code/tests, that `sp_mf_call_submit` must validate these new child
  inputs with the SAME arg-shape `SIGNAL`s `sp_mf_workflow_create_planned` already uses for the identical
  fields, before anything else runs: `arg_child_plan_length IS NULL OR < 1` → `SIGNAL`; `arg_child_continuation`
  not a valid JSON OBJECT → `SIGNAL`; `arg_child_event_payload` not a valid JSON OBJECT → `SIGNAL`;
  `arg_child_next_attempt_at IS NULL` → `SIGNAL`; `arg_input_json` (the child's future `args_canonical`) not
  a valid JSON OBJECT → `SIGNAL` — the exact same entry-check tier `sp_mf_workflow_create_planned` runs for
  `arg_plan_length`/`arg_continuation`/`arg_event_payload`/`arg_next_attempt_at`/`arg_args` today (all
  `SIGNAL`s, all before the fence/idempotent/recursion-guard phase in step 1-3 above, matching how every
  other SP in this codebase puts arg-shape SIGNALs first, structured outcomes second). Carry this into the
  SP regression test list too (round-3/round-4 already added submit-idempotency + zero-partial-rows-on-
  rejection tests — add one more: each new child-input SIGNAL fires on its corresponding malformed input).
  `input_json` is DOUBLE-duty (round-2 finding #1): it is both the call operation's `input_json` AND, in the
  SAME call, canonicalized into the child's `tb_mf_workflow_args.args_canonical` — one caller-supplied value,
  two rows, one transaction; `CallConflict` now also covers an args-byte mismatch on replay.
- `call_inspect(workflow_id, operation_seq) -> CallInspectOutcome` — PURE read (finding #2): child's
  authoritative state/return + `child_workflow_id` + the sidecar's (possibly-stale) `child_status` hint.
- `call_hint_refresh(workflow_id, operation_seq, child_status, event_ts) -> Void` (NEW, split out of the
  old call_inspect design per finding #2) — explicitly best-effort, no outcome variants worth modeling
  (failure here has zero correctness impact by construction).
- `checkpoint_reverse_noop(workflow_id, fencing_token, seq, event_ts, event_payload) -> CheckpointReverseNoopOutcome`
  (NEW, finding #1) — mirrors `ReverseSettleOutcome`'s existing variant shape (`Reversed`/`AlreadyReversed`/
  `OutOfOrder`/`FenceLost`/...), plus `NotCallCheckpoint` for the defensive `call_kind` guard.
- `child_terminal_notify(child_workflow_id, event_ts) -> Void`/`NotifyOutcome` — **sequencing resolved**:
  invoked as a SEPARATE call strictly AFTER the child's own terminal-settle transaction has already
  committed, never nested inside it (see the sequencing note under `sp_mf_child_terminal_notify` above);
  fire-and-forget, best-effort, since `call_inspect`/poll is the correctness floor regardless.

### Open items for the runner-runtime pass (item 3, NOT part of this SP/schema plan)
The exact `NeedCall`/`advance()` IR-side wiring (DESIGN.md already specifies `StepOutcome::NeedCall` +
`_node_depths` counting a call as `depth+1` — both currently absent from `ir.drift`, confirmed), the
`_run_reversal` branch consuming the new `call_kind` field (sketched above but not yet code), and the exact
runner call site that triggers `child_terminal_notify` post-commit are deferred to the runner-runtime design
pass once this SP/schema plan is signed off.

## 1b.1 — schema/SP implementation (LANDED, green)

The SP/schema plan above (round 5, fully reviewed) is now implemented and passing. Scope matches
exactly what was green-lit: (1) durable schema, (2) the 5 new SPs + 1 extended SP. Runner/host
wiring (item 3) is explicitly NOT part of this pass — deferred to its own design/implementation
round, as scoped from the start.

**Schema**: `microflows/db/schema/tb_mf_workflow_operation.sql` (+`call_kind` TINYINT +
`ck_mf_operation_call_kind`), `microflows/db/schema/tb_mf_workflow.sql` (+4 ancestry columns +
`ck_mf_workflow_ancestry`), new `microflows/db/schema/tb_mf_workflow_operation_call.sql` (the
`tb_mf_call` sidecar — named with the `tb_mf_workflow_operation_` prefix purely so Mariachi applies
it after both FK parents, same trick `tb_mf_workflow_operation.sql` itself already uses). Migration
`microflows/db/migrations/0005_workflow_call.sql` mirrors the schema files for an already-deployed
install — no backfill needed anywhere (every pre-existing row is, by construction, a top-level
workflow / a participant operation, satisfying both new CHECK constraints with zero data changes).

**SPs**: `sp_mf_call_submit`, `sp_mf_call_inspect`, `sp_mf_call_hint_refresh`,
`sp_mf_child_terminal_notify`, `sp_mf_checkpoint_reverse_noop` (all new), plus
`sp_mf_checkpoint_reverse_head` extended (`Pending` outcome gains `call_kind`, defaulting to 1 if
the operation row is somehow absent — defensive, never blocks existing behavior).

**Two implementation-time refinements beyond the reviewed plan, both additive/consistency fixes,
neither a design change:**
1. **Plan-order conformance added to `sp_mf_call_submit`.** The reviewed plan didn't explicitly
   call this out, but since a call op occupies a call-site position within the PARENT's own plan
   exactly like a participant op, the SAME durable ordering rules `sp_mf_operation_request` already
   enforces (`operation_seq` in `[1, plan_length]`, predecessor settled) apply here too — added,
   mirroring that SP's exact logic and ordering (checked before the existing-row/idempotency check,
   same as `operation_request`).
2. **`operation_id`/`child_workflow_id` equality is an ENFORCED SIGNAL (`MfOperationIdChildMismatch`),
   not a silent derivation.** The reviewed host signature listed both as separate parameters (mirroring
   `operation_id` always being caller-supplied, never derived, throughout this codebase); the SP
   entry-validates that they agree rather than silently trusting or overwriting one from the other.

**Recursion guard**: implemented via a `WITH RECURSIVE` CTE (confirmed safe — the repo's pinned dev/test
MariaDB fixture is 11.4, well past the 10.2 minimum for recursive CTEs). Walks `parent_workflow_id`
links from the immediate parent upward (hops bounded by `max_call_depth`), joining
`tb_mf_workflow_plan` at each ancestor, comparing against the child's own `(script_name, plan_version,
content_hash)` — the base case (hops=0) is the parent itself, so a direct self-cycle (A calls A) is
caught by the same query, no special-casing needed.

**Tests**: new `microflows/db-tests/sp_call_test.py` (77 checks, mirrors `sp_operation_test.py`'s
`check()`/`call()`/`EXPECTED_CHECKS`-completeness-guard pattern exactly), wired into
`microflows/justfile`'s `_test-sp` alongside the existing file. Covers every minimum pin requested:
fresh submit's full atomic bundle (op row, child workflow/plan/args/created-event, sidecar, parent
continuation/event — verified column-by-column via direct `SELECT`s, not just the SP's own JSON
outcome), every new child-input SIGNAL firing with zero rows left behind, both recursion-guard
rejection paths (`call_cycle` via a manufactured self-cycle, `max_call_depth_exceeded` via a genuine
2-hop nested call) each leaving zero partial rows, idempotent replay vs `call_conflict` on each
individual immutable field, `call_inspect`'s pure-read property (snapshotted before/after, byte-
identical), `call_hint_refresh`'s monotonic-by-time no-clobber behavior, `child_terminal_notify`'s
strict scope (hint + wake only — parent state/event-count/op-row snapshotted before/after, confirmed
unchanged), `reverse_head` surfacing `call_kind` for both a call and a participant checkpoint, and
`checkpoint_reverse_noop`'s full state machine (reverses a call checkpoint, idempotent replay,
rejects a participant checkpoint outright, out-of-order rejection, descend-to-next-checkpoint).

**Verification**: `microflows`'s full `just test` green — `sp_operation regression: 156/156` (no
regressions from the schema changes), `sp_call regression: 77/77` (new), parser fixtures 100/100,
manifest fixtures 8/8, all unit tests, `JUST_EXIT=0`.

**Next**: host wrappers (`call_submit`/`call_inspect`/`call_hint_refresh`/`child_terminal_notify`/
`checkpoint_reverse_noop` on `MicroflowsHost`) + the runner-runtime pass (item 3: `NCallWorkflow`
dispatch, `StepOutcome::NeedCall`, `_run_reversal`'s `call_kind` branch, the post-commit
`child_terminal_notify` call site) — per the user's own sequencing, review of this landed schema/SP
pass comes first.

**Post-landing review found 3 more gaps, all fixed, `sp_call_test.py` now 79/79**: (1) MEDIUM —
`sp_mf_call_submit`'s idempotent-replay check compared the sidecar's plan-identity triple
(`script_name`/`plan_version`/`content_hash`) but never the child's `plan_length`, even though the
durable plan pin is all four fields together and the fresh-submit write phase DOES write
`plan_length` from `arg_child_plan_length`. Fixed: reads `tb_mf_workflow_plan` for the child on
replay, includes `plan_length` in the agreement check, treats a missing plan row as `call_conflict`
(same as a missing sidecar/args row) — pinned by `submit_conflict_plan_length`. (2) MEDIUM —
canonicalization of the child's args was undocumented at this layer (the SP stores `arg_input_json`
verbatim into `args_canonical`, with no host wrapper written yet to canonicalize it first). Fixed:
the SP's header + parameter comment now explicitly states the same division of responsibility
`sp_mf_workflow_create_planned` already documents for its own `arg_args` — canonicalization is the
CALLER's job, never this SP's — and a new regression test (`submit_noncanonical_json_not_idempotent`)
pins this by proving a byte-different-but-semantically-equal JSON document is rejected as
`call_conflict`, not silently treated as `already_submitted`. (3) LOW — a stale comment in
`sp_mf_child_terminal_notify` said "blocked (1, non-terminal)"; `child_status` code 1 is `pending`,
`blocked` is 4 — comment corrected.

## 1b.1 — host API wrappers (LANDED, green; runner-runtime wiring deliberately deferred)

Implemented the typed `MicroflowsHost`/`HostImpl` wrappers for the SP surface that landed above:
`call_submit`, `call_inspect`, `call_hint_refresh`, `child_terminal_notify`, `checkpoint_reverse_noop`,
plus extending `ReverseHeadOutcome::Pending` with `call_kind` (both the variant and
`HostImpl.reverse_head`'s decode). All in `microflows/packages/microflows/src/host.drift`, mirroring
existing conventions exactly: `_call_sp_doc`/`_finish_stmt_and_commit`, `_doc_int_req`/`_doc_str_req`/
`_doc_str_opt`/`_doc_object_text_opt` decode helpers, `ManagedError` -> `HostErrorKind::BackendUnavailable`,
client-side arg validation mirroring each SP's own entry SIGNALs, and `_canonical_args` reused as-is to
canonicalize the call's input before `call_submit` (the same function, and the same reasoning, `create_planned`
already uses for its own `args_json` — the call's input literally becomes the child's instance arguments).
`call_submit`'s outcome variants and their field shapes (`Submitted`/`AlreadySubmitted`/`CallConflict`/
`CallRejected(reason)`/`PlanViolation(reason, plan_length)`/`EventTimeSkew(defer_until)`/`FenceLost`/
`NotFound`) match the SP's JSON outcomes exactly, verified field-by-field against the SP source. `executor`
is NOT a host-method parameter for `checkpoint_reverse_noop` (unlike the SP, which does take it) — read
from `self.identity.executor_id` instead, matching every other fenced host method in this file; this is a
deliberate deviation from a literal reading of the request's SP-shaped signature list, in favor of "mirror
existing host conventions" (also explicitly requested).

**Two real compile breaks found and fixed, both from the same root cause (`ReverseHeadOutcome::Pending`
gaining a 4th field) reaching further than expected:**
1. `microflows/packages/microflows/tests/e2e/live_reversal_test.drift`'s own `Pending(seq, opname,
   payload)` match arm needed the new `call_kind` binder — fixed, asserting `call_kind == 1` since that
   fixture's checkpoint is a participant op.
2. `microflows/runner/src/runner.drift`'s `_run_reversal` (the actual PRODUCTION consumer of this host
   method) has its own `Pending(...)` match arm — also broke. Fixed MINIMALLY: added the 4th binder
   (`_call_kind`, underscore-prefixed, explicitly unused) with a comment stating the real branching (skip
   `_compensation_for` + call `checkpoint_reverse_noop` for `call_kind=child_workflow`) is runner-runtime
   work, deliberately deferred to its own pass — this fix only keeps the match exhaustive; `_run_reversal`'s
   actual behavior is byte-for-byte unchanged (every checkpoint still goes through the existing
   participant-only compensation path).

**New focused e2e test**: `microflows/packages/microflows/tests/e2e/live_call_test.drift`, mirroring
`live_reversal_test.drift`'s structure (same `_build_host`/`_wf_id`/synthetic-time conventions), wired into
`microflows/tools/emit_test_plan.py`'s `LIVE_TESTS` list. Deliberately NOT exhaustive — the underlying SP
logic already has 79 passing checks (`db-tests/sp_call_test.py`); this file instead proves the HOST-LAYER
wiring (arg marshaling in the exact SP parameter order, outcome-string-to-variant decoding, SP name/
signature agreement) by exercising every new method's happy path + 1-2 key branches, including a real
end-to-end construction of a reversing workflow with an ACTIVE call checkpoint via the actual host call
chain (`create_planned` -> `claim_workflow` -> `call_submit` -> `operation_settle`(intermediate) ->
`operation_request` -> `begin_reversal` -> `reverse_head` -> `checkpoint_reverse_noop`) rather than a new
seed proc — this incidentally also proves `call_kind` flows correctly end-to-end from `sp_mf_call_submit`'s
op-row write through `sp_mf_checkpoint_reverse_head`'s decode. One implementation-time lesson worth
recording: `begin_reversal` RETAINS the caller's lease when there's an active checkpoint to unwind (transitions
to `Reversing`, not the empty-stack `Failed` case) — no re-claim is needed or possible immediately after
(the lease is still held), a detail that cost two debugging round-trips against the live DB before landing.

**Verification**: full `microflows` `just test` green — 25 unit/e2e jobs (0 failed, including the new
`live_call_test` and the extended `live_reversal_test`), parser fixtures 100/100, manifest fixtures 8/8,
`sp_operation_test.py` 156/156 (no regressions), `sp_call_test.py` 79/79, `JUST_EXIT=0`.

**Deliberately out of scope for this pass** (per the user's explicit "hold before runner runtime wiring so
we can review the host API shape and outcome decoding"): `NCallWorkflow` dispatch, `StepOutcome::NeedCall`,
`_run_reversal`'s actual `call_kind` branch (currently a no-op placeholder, see above), and the post-commit
`child_terminal_notify` call site. Review of this host-API pass comes first.

**Host-API review found 2 gaps, both fixed:** (1) MEDIUM — `call_submit` canonicalized `input_json`
internally but still took a separate caller-supplied `input_hash`, creating a hidden invariant (the caller
had to independently canonicalize identically to the host's own canonicalization, or the hash would
silently describe a different document than what actually got stored). Fixed by making the host COMPUTE
`input_hash` itself from the canonical bytes it already produces — confirmed feasible with zero blast
radius (`call_submit` has no callers yet anywhere in `runner.drift`, so the signature was free to change).
Added `_canonical_input_hash` (new private helper in `host.drift`, right next to `_canonical_args`), which
replicates `runner.drift`'s own `_input_hash` algorithm exactly (canonical text -> UUIDv3 -> lowercase hex,
via `uuid.v3_from_string`/`uuid.to_bytes`/`codec.hex_encode` — all already imported in `host.drift`, no
build-config change needed) — this keeps every `input_hash` in the system produced the same way, not just
internally self-consistent within `call_submit`. Removed `input_hash` entirely from `call_submit`'s
parameter list (both the `MicroflowsHost` interface declaration and the `HostImpl` method) and its
`_validate_nonempty` check. (2) LOW — `live_call_test.drift`'s replay-test comment claimed the input was
"already ordered-key compact," which was both inaccurate (it had a space after the colon) and, after fix
(1), moot (there's no caller-supplied hash left to justify anymore). Turned this into an actual positive
proof instead of a stale comment: the replay call now uses a DIFFERENTLY-formatted but semantically-
identical JSON document (`{"amount":10}` vs the fresh submit's `{"amount": 10}`) and still asserts
`AlreadySubmitted` with the same child id — a genuine end-to-end demonstration that the host canonicalizes
before hashing/comparing, not an assertion resting on the input already happening to be canonical.

**Re-verification**: `host.drift` + the updated `live_call_test.drift` both compile clean; the runner
rebuilds clean (confirming zero callers were broken); full `microflows` `just test` re-run green — 25
unit/e2e jobs (0 failed), parser fixtures 100/100, manifest fixtures 8/8, `sp_operation_test.py` 156/156,
`sp_call_test.py` 79/79 (unaffected — only the host changed, the SP's own `arg_input_hash` parameter is
untouched), `JUST_EXIT=0`.
