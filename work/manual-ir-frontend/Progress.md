# manual-ir-frontend — Progress / status

Charter (objective, decisions, plan, verification): [README.md](./README.md).

## Status: **charter created — not started**

The lane is scoped (IR-first, parser-last: prove control-flow + durable restart on manual
IR before any syntax work). No code yet.

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
- [ ] **1 — Typed workflow IR + durable arguments** in `ir.drift` (+ `db`/`host`). Value model
  + control-flow nodes (`Operation`, `If`/`Case`, finite `Loop`, `Let`, `Return`/terminal);
  flat `PlanStep` plan as a degenerate straight-line graph; `content_hash` over the graph
  (incl. the declared arg type); **durable args** child (`tb_mf_workflow_args` VARBINARY
  canonical bytes + `create_planned` arg/byte-compare `workflow_conflict` + `args_get` + host).
  **NEXT.**
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
Begin **Step 1**: define the typed IR in `microflows/runner/src/ir.drift`, re-express the flat
plan as a degenerate straight-line graph (current suites stay green), extend `content_hash`
over the graph, AND add durable arguments — the immutable `tb_mf_workflow_args` child written
atomically with the workflow + plan pin (`create_planned` + `args_get` + host variants,
`workflow_conflict` on differing canonical content; resume reads the durable record). The args
child is the only new durable state; pure control flow persists nothing.

## Open questions (see README)
How far back replay restarts (last settled op vs start) · `../drift-lang` reuse for step 5.
