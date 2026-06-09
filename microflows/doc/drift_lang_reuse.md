# drift-lang Reuse Analysis

> **Note (2026-06-07):** PhaseDrift was renamed **Microflows** and narrowed to
> a workflow coordinator (see `microflows_design.md`). This reuse analysis is
> unaffected — Microflows still parses/type-checks/verifies into a versioned
> portable interpreted IR and reuses the same drift-lang patterns. The one
> change: the mariadb-rpc free-SQL (`exec`) question is **withdrawn** —
> Microflows persists only control state via stored procedures and never needs
> generic business-record SQL.

**Status:** working note
**Purpose:** record which `../drift-lang` components PhaseDrift reuses
directly, which patterns it mirrors, where it intentionally deviates, and
which reuse boundaries need toolchain-team guidance.

Reference roles (per project direction):

- `../drift-lang` — language/compiler/IR architecture.
- `../pushcoin/singular` — project layout, build/test conventions, dependency
  integration.
- `doc/phase_drift_mile_design.md` — runtime semantics and host contracts.

## The boundary fact that shapes everything

`driftc` is implemented in **Python** (Lark parser, dataclass AST/HIR/MIR/SSA,
LLVM-native codegen; `lang/driftc/driftc.py`). It has **no interpreter** (the
early tree-walk interpreter was removed; `doc/articles/driver-notes.md`), and
its compiler components are **internal-only** — none are exposed as certified
Drift libraries.

PhaseDrift's parser and runtime are implemented **in Drift** (milestone-1
decision) and execute a **versioned portable interpreted IR** (design doc
§22). Direct *code* reuse across the Python/Drift boundary is therefore not
possible for the compiler stack. Reuse happens at three other levels:
**tooling** (used as-is), **formats/specs** (adopted as contracts), and
**architecture patterns** (mirrored in Drift).

## Reused directly (as-is)

| Component | Source | Use in PhaseDrift |
|---|---|---|
| Shared test executor | `$DRIFT_TOOLCHAIN_ROOT/lib/tools/drift_test_run.py` | All gates run on it via `tools/emit_test_plan.py` (already wired) |
| Package manifest/lock/trust | `lang/driftc/packages/{manifest,verify_v1}.py`, `doc/design/trust-v1.md` | `drift/manifest.json` (schema 2), `drift/lock.json` (schema 4), `drift/trust.json`, author-claims — adopted wholesale at scaffold |
| `drift prepare` / `drift author` / `drift deploy` CLI | toolchain `bin/drift` | Dependency resolution and (later) signed script-artifact publication |
| Versioning discipline | `lang/versions.py` | Dual scheme adopted: `PHASEDRIFT_VERSION` (semver, behavior) + `PD_IR_FORMAT_VERSION` (integer, bumped only for IR shape changes) — mirrors `DRIFTC_VERSION` / `DRIFT_RT_ABI_VERSION` |

## Adopted as format/spec contracts (reimplemented in Drift)

1. **Deterministic canonical JSON** (`lang/driftc/packages/provisional_dmir_v0.py`,
   `dmir_pkg_v0.py`): sorted keys, `_type` discriminator for tagged nodes,
   enums by name, no floats in canonical payloads (PhaseDrift bans floats in
   financial data anyway), source-root-relative path normalization.
   PhaseDrift's IR serialization follows the same rules so that byte-identical
   IR ⇒ identical content hash — required for immutable script revisions
   (design doc §22) and any future signing.
2. **DMIR's architectural position** (`doc/design/dmir-spec.md`): a
   *post-type-check, canonical, versioned, signable* module representation
   with structured control flow. PhaseDrift IR sits in exactly this slot of
   the pipeline (it is the deploy/interpretation artifact), with a version
   header and a verification pass before registration.
3. **Diagnostics conventions**: stable `E_<SUBSYSTEM>_<ISSUE>` error codes;
   the `Located` span shape (`line, column, file, end_line, end_column,
   start_pos, end_pos`) for source positions (`lang/driftc/parser/ast.py`).
   PhaseDrift diagnostics use the same code style (`E_PD_...`) and span
   fields so tooling treats both alike.

## Mirrored patterns (adapted to PhaseDrift semantics)

1. **Stage pipeline.** drift-lang: AST → HIR (sugar-free) → MIR (CFG) → SSA →
   LLVM. PhaseDrift: AST → typed/bound HIR-equivalent → **portable IR**
   (interpreted; no MIR/SSA/native stages). The collapse is deliberate:
   PhaseDrift IR must carry *resumable positions* for durable continuations
   (§4.1), which favors structured, position-stable IR over optimized CFG/SSA
   forms.
2. **Grammar-first.** A formal grammar document (mirroring
   `doc/design/drift-lang-grammar.md`) stays the source of truth even though
   the PhaseDrift parser is hand-written recursive descent in Drift (no Lark
   equivalent exists in Drift).
3. **Central TypeTable + integer TypeId** (`lang/driftc/core/types_core.py`)
   for the PhaseDrift type checker, including the substitution pattern for any
   future generic schemas.
4. **Event-sink observability** (`lang/driftc/_events.py` in spirit): runtime
   instrumentation as optional callback sinks, the same idiom mariadb-rpc /
   Singular already use in Drift (`event_sink` on pool/gateway builders).

## Intentional deviations (and why)

1. **Implementation language: Drift, not Python.** PhaseDrift embeds in host
   services, hot-deploys scripts like stored procedures, and ships as a
   certified Drift library. driftc's Python stack targets offline native
   compilation — a different deployment shape.
2. **An interpreter exists.** drift-lang deliberately has none; PhaseDrift's
   runtime *is* an interpreter. Durable continuations must reference stable IR
   positions across process restarts and executor handoffs — native frames
   can't be persisted. Tree-walking over verified IR is the milestone-1
   execution model; a bytecode form can come later without changing the IR
   contract version discipline.
3. **Workflow-shaped IR, not DMIR.** PhaseDrift IR's node vocabulary is
   workflows/phases/guards/checkpoints/continuation-positions, not
   general-purpose function bodies. We adopt DMIR's serialization,
   determinism, and versioning rules without its instruction set.
4. **Hand-written parser.** No parser generator is available in Drift;
   recursive descent against the grammar doc, with golden-file tests (the
   drift-lang test conventions still apply).
5. **Open event-kind vocabulary in `tb_pd_workflow_event.kind`** during the
   runtime-spine build (steps 1–6), hardening to a CHECK once the set
   stabilizes — Singular pins codes immediately; we defer until reversal and
   resolution land.

## Focused questions for the toolchain team

1. **DMIR alignment.** `dmir-spec.md` says the format may evolve before the
   first signing milestone. Should PhaseDrift align its IR *envelope*
   (header/version/signing wrapper) with DMIR so script revisions can ride the
   same trust-v1 signing/verification path later — or is `provisional_dmir_v0`
   considered private and we should design an independent envelope under
   trust-v1 only?
2. **Signed third-party artifact kinds.** Is `drift author` / the cert-claim
   flow intended to sign artifacts that are not Drift packages (e.g., a
   PhaseDrift script revision as its own artifact kind), or should script
   revisions be wrapped as assets inside a normal Drift library artifact?
3. **Diagnostics contract.** Are the `Located` span shape and `E_*` code
   convention a public contract (IDE/tooling-facing) we should match exactly,
   or internal conventions we may diverge from?
4. **Stdlib support for parser/interpreter work in Drift.** Recommended idioms
   (or planned additions) for: UTF-8 scanning/cursor over `String`, hash maps
   for symbol tables (`std.containers` surface), and an eventual fixed-scale
   `Decimal` for money (milestone 1 uses `Int` cents).
5. **Deterministic time/randomness.** Design doc §14.1/§25.9 needs
   invocation-scoped time/random values. Does the Drift runtime plan a
   virtual-clock/seeded-random facility worth targeting, or should the
   MariaDB host remain the sole authority (DB time, DB-generated entropy)?

## Current adoption status

- Scaffold, manifest/lock/trust, test gates: **done** (mirrors Singular).
- State machine + claimability (step 1): **done**, unit-tested.
- MariaDB schema (step 2): **done**, loaded against mdb114-a.
- IR format work (step 4+) will apply the canonical-JSON contract above;
  questions 1–2 should ideally be answered before the IR envelope freezes
  (step 4 can proceed with a local envelope and adopt the answer at the
  parser milestone).
