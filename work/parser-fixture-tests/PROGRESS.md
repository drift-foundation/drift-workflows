# Data-driven Mf parser tests — PROGRESS

See `PLAN.md`. Toolchain: staged `~/opt/drift/staged/toolchain/drift-0.33.54+abi18` (until cert team
makes it default; it fixes the codegen OOM bug that `parser_test` tripped on 0.33.53).

## Status: CONVERSION DONE — verifying full runner gate

Data-driven parser suite landed. `--parse-check` mode added to the runner binary; 74 check + 3 lower
fixtures + committed goldens; harness `tests/run_parser_fixtures.py`. **77/77 in 0.082 s** (vs
~5.4 GB / ~4 min PER VARIANT for the old compiled parser_test). Goldens cross-checked against the old
test's assertions (diagnostic codes+byte/line/column incl. multibyte café, is_graph, validate/type
rejects, rename→identical-canonical parity, lower overlay/strip/unknown-op). Heavy compile preserved
off-path as `just test-compiler-stress`.

## Established facts

- `parser_test.drift` compile = **5.4 GB / 3m55s** peak on 0.33.54 (was OOM/137 on 0.33.53 — driftc bug,
  now fixed). Cost driver = the inlined `scenario()` (520 lines, ~60 inlined `.mf` scenarios, 170 generic
  sites) in one translation unit. Not legitimate for a daily/cert gate.
- The shipped `microflows-runner` binary already supports `--lower-source FILE --config BASE.json`
  (stdout = lowered config JSON), `--emit-content-hash`, and structured parse diagnostics (stderr std.log
  event, stable `code` + line/col, exit 3). → data-driven testing needs no new binary infra (maybe a
  small diagnostic-JSON emit mode if stderr parsing is brittle).

## Checklist

- [x] Confirm parser_test compiles on 0.33.54 (5.4 GB, exit 0) + identify cost driver.
- [x] Confirm the runner binary's `--lower-source` / `--emit-content-hash` / structured-diagnostic surface.
- [x] Write PLAN.md + PROGRESS.md.
- [x] Add `--parse-check FILE` to the runner binary (canonical JSON outcome; DB/registry-free). Built clean on 0.33.54 (peak ~2.5 GB — the real product, built once).
- [x] Inventory `scenario()` cases (704-line test) → 74 check + 3 lower fixtures.
- [x] Create fixture corpus (`tests/fixtures/parser/{check,lower}/*.mf` + base configs).
- [x] Write `tests/run_parser_fixtures.py` (run + `--update`).
- [x] Generate + bless goldens; harness green (77/77, 0.082 s). Goldens cross-checked vs old assertions.
- [x] Wire runner `just test` → build-once + fixtures; move compiled `parser_test` to `just test-compiler-stress`; fixed `just build` artifact selector.
- [ ] Verify full runner `just test` green on 0.33.54 (ir tests + fixtures) — IN PROGRESS.
- [ ] (optional) note in cert PROGRESS/README that the parser gate is now data-driven (no multi-GB compile).
- [ ] Decide: keep ir_graph/ir_exec compiled (ir-only, not the 5 GB problem) — left as-is for now.

## Notes

- Keep coverage 1:1; bless goldens from the currently-passing test.
- Heavy compiled test is KEPT as a canary (caught a real driftc bug) — just off the daily/cert path.
