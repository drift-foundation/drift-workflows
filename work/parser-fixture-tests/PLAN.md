# Data-driven Mf parser tests — PLAN

## Problem

`microflows/runner/tests/unit/parser_test.drift` (704 lines) is a single ~520-line `scenario()`
function that **inlines ~60 `.mf` scenarios as Drift string literals plus their hand-authored expected
configs**, then asserts in-Drift. Compiling it costs **~5.4 GB peak / ~4 min per variant** (base + asan
= the heavy part of `just test`). On `driftc 0.33.53` it didn't just run heavy — a codegen bug blew it
past 30 GB → `Killed (137)`. `0.33.54+abi18` (staged at `~/opt/drift/staged/toolchain/`) fixes the bug
and bounds it to 5.4 GB, but **5 GB to test a parser is not legitimate for a daily/cert gate**.

The cost is 100% the test *architecture*, not anything real about microflows: the scenarios + expected
configs + 170 generic-instantiation sites all get monomorphized into one giant function in one
translation unit. At runtime the parser handling a small `.mf` file costs kilobytes.

## Direction (owner)

> "Produce the Mf parser/compiler which, after it becomes a binary, reads these tests."
> "It doesn't matter how long it takes to build the runner — we have to have an mf-compiler at some point."
> "We cannot eat 5 GB."

Standard compiler-test pattern: **build the Mf compiler binary once (it's the shipped product anyway),
then feed scenarios as data files at runtime and golden-compare.** Adding a test = adding a fixture
file, zero recompilation.

## Key finding — the binary ALREADY does this

`microflows-runner` (`src/runner.drift:297` `main(argv)`) already exposes the needed modes:

- `--lower-source FILE --config BASE.json` — read a `.mf` file, run `parser.lower`, **validate through
  the real build path**, print the merged/lowered config JSON to stdout, exit 0 (`_lower_source`,
  `runner.drift:397`). The comment calls it "the textual frontend."
- `--emit-content-hash --config CFG.json` — print a revision's `content_hash` hex
  (`_emit_content_hash`); comment: "used to (re)compute fixtures."
- On a parse error: emits a **structured diagnostic** (stable kebab `code` + byte_offset/line/column) as
  a std.log JSON event on stderr + a human caret render, exit 3.
- On a semantically-invalid lowered config: exit 3 with a message.

So no new infra is strictly required — only fixture extraction, a thin harness, and gate wiring.

## Target architecture

- **Daily / cert `just test`:** build the runner binary once → run the fixture harness (cheap, KB RAM).
- **Off-path canary:** keep the heavy compiled `parser_test.drift` as `just test-compiler-stress`
  (NOT in the daily/cert gate). It is proven to catch driftc codegen regressions — that value is kept,
  just moved off the critical path.

### Fixture layout (`microflows/runner/tests/fixtures/parser/`)

```
_base.json                 # shared base routing registry (superset of ops: reserve/release/ping/op1/pack/...)
ok/<name>.mf               # a scenario that must parse + lower + validate
ok/<name>.expected         # golden: canonical lowered-config JSON (or its content_hash)
ok/<name>.base.json        # OPTIONAL per-fixture base (falls back to _base.json)
err/<name>.mf              # a scenario that must FAIL
err/<name>.expected        # golden: { "code": "<kebab>", "line": N, "column": M } (exit 3)
```

### Harness (`microflows/runner/tests/run_parser_fixtures.py`, python3, no extra deps)

- Walk `ok/` and `err/`; for each, run the built binary with the matching base.
- `ok`: capture stdout, canonicalize JSON, compare to golden (or compare `--emit-content-hash`).
- `err`: assert exit 3 + parse the structured diagnostic from stderr; compare `code` (+ line/col) to golden.
- `--update`: (re)generate goldens from the binary. Same blessed-baseline pattern as `perf/baselines`.
- Exit nonzero with a per-fixture diff on any mismatch.

## Coverage & correctness

1:1 with the current `scenario()` cases. Goldens are **blessed from the currently-passing test** — at
conversion time the hand-authored parity assertions still hold, so the generated golden *is* the intended
output. Thereafter the golden catches regressions (the same guarantee the old test gave, minus the
hand-authored re-derivation). The lowered-config golden embeds the canonical `argument_type` / contracts /
graph, so the old `_objtype_canon` / `_graph_canon_of` parity checks are subsumed by the config diff.

## Risks / caveats

- **Structured diagnostic is on stderr as a std.log event.** Decision: parse that line (stable
  `code`/position fields). If it proves brittle, add a focused stdout `--emit-diagnostic-json` mode to the
  binary (small, localized change).
- **Base-registry coupling:** lowering needs a base routing config; pure-syntax parse errors fire before
  routing matters, so they work against any base. Semantic cases need the right ops in the base.
- **`content_hash` vs full-config golden:** prefer the full lowered-config JSON as the golden (richer
  diff on failure); keep `--emit-content-hash` as a cross-check for the parity-specific cases.

## Out of scope

- The `ir_graph_test` / `ir_exec_test` (already lighter, ir-only). Revisit only if they're also heavy.
- Changing the parser/IR logic. This is a test-architecture change only.
- Editing build-orchestrator (cert constraint unchanged).
