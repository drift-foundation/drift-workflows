# driftc 0.33.91 / ABI 22 migration — reject-redundant-call-borrows

## Short-term objective

Align drift-workflows (singular 0.9.x, microflows 0.8.x, uflowsd 0.7.x) with
staged `driftc 0.33.91 | abi 22 | git ef7ebd14`
(`DRIFT_TOOLCHAIN_ROOT=/home/sl/opt/drift/staged/toolchain/drift-0.33.91+abi22`),
run all gates, reseal, stage a patch release, and publish the announce note.

## Current behavior / problem

0.33.91 is a source-compat break: a source-written borrow in an argument
position whose formal is DECLARED `&T`/`&mut T` is rejected
(`E_REDUNDANT_ARG_BORROW`). Also: `E_MUT_RVALUE_ARG_BINDING_REQUIRED`
(mutable temporaries must be bound first) and
`E_OVERLOAD_PARAM_MODE_ONLY_DIFF` (mode-only overload sets rejected at
definition). Our sources are written in the pre-rule spelling.

## Accepted design decisions

- Migrate strictly from compiler diagnostics (driftc spans), never regex/textual
  sweeps; recompile to convergence (earlier type errors can mask later ones).
- Preserve borrows that still do work: &Concrete→&Interface coercions,
  generic-by-value reference instantiation (FnN/CallbackN.call), thin
  fn-pointer calls, constructor fields, capture lists, non-argument borrows.
- Shipped docs migrate to the new spelling; documented compiler floor raised to
  driftc 0.33.91+/ABI 22. Previous release (bc763fb) was already ABI 22
  (0.33.88), so no ABI-21-rebuild callout needed beyond the standard
  pre-rule/post-rule ABI 22 compatibility statement.
- Patch version bumps only (no API/behavior change): follow root
  drift/manifest.json as sole version authority; keep RUNNER_VERSION in sync
  (emit_test_plan.py preflight enforces).
- Any suspected checker/lowering/codegen/runtime misbehavior exposed by the new
  compiler = CORE_BUG: stop, minimal repro, report; no source-level workaround.
- Deps: mariadb-rpc/wire-proto (staged 0.8.1/0.6.1) and net-tls (staged 0.6.3)
  are compiled packages — pre-rule artifacts stay readable, so re-pinning is a
  reseal-time decision, not a build blocker.

## Concrete implementation plan

1. Compile everything with the staged toolchain; collect the three diagnostics.
2. Fix per-span; recompile to convergence.
3. Migrate shipped doc examples; raise doc floor to 0.33.91+/ABI 22.
4. Gates: `just test`, `just test-resilience`, `just stress`, `just perf`,
   `just trust-check` (+ packaging legs as applicable).
5. Bump versions (patch) + reseal (author-claims, prepare/lock, trust-check).
6. Commit (with explicit user permission — git is read-only for the agent),
   THEN build/stage artifacts so provenance names the final commit.
7. Verify SCI agreement (provenance / author-claim / cert-claim).
8. Publish /tmp/drift-announce/<ISO-UTC>-drift-workflows-release-notes.md.

## Files likely affected

Drift sources under singular/drift/, microflows/packages/, microflows/runner/,
microflows/participant-stub/, integration/*/; shipped docs (microflows/doc/,
README.md files); drift/manifest.json + author-claims + lock.json.

## Verification criteria

All root gates green under the staged toolchain; zero remaining migration
diagnostics; trust-check green; staged artifacts' SCI consistent; announce
published.

## Current status and next action

See PROGRESS.md.
