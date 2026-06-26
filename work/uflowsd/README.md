# uflowsd — certified runnable Microflows daemon

## Short-term objective

Deliver `uflowsd`, the Microflows **daemon** (long-running HTTP front-door that drives `.mf` plans +
dispatches operations to participants), as a **certified, verify-gated, runnable artifact** consumable
from the certified path — so a cert-only consumer (pushcoin) can run `.mf` flows without a drift-workflows
source checkout. Build + locally verify now; certify the moment the toolchain supports app cert.

## Current behavior / problem

The runnable service exists only in source: `microflows/runner` ships two `kind:app` artifacts —
`microflows-runner` (CLI one-shot, `::main`) and `microflows-service` (the daemon, `::service_main`).
Neither is published/certified. pushcoin asked for a runnable, verify-gated `microflows-daemon` from the
certified path (ref `2026-06-25T17:41:04Z-pushcoin-release-notes.md`).

## Roles / naming (settled)

- **engine** = the `microflows` **library** (package name stays `microflows@0.2.0`; "engine" is its role) —
  reusable in-app / embedded / as the daemon's basis. Already certified.
- **CLI** = `microflows-runner` (one-shot) — unchanged.
- **daemon** = **`uflowsd`** — renamed from `microflows-service` (one name per role).

## Accepted design decisions

- **Rename `microflows-service` → `uflowsd`** everywhere (manifest artifact + integration refs + docs +
  the daemon's own log/listening strings). Entry point stays `microflows.runner::service_main`; module
  namespace stays `microflows.runner`; only the artifact NAME + the daemon's identity strings change.
- **Logging mirrors PushCoin Bookkeeper** (`/home/sl/src/pushcoin/bookkeeper/src/{logging,app,routes/submit}.drift`):
  - a `uflowsd.logging` module: a `log.ContextResolver` over VT-scoped `rt.thread_registry()` state
    (`ScopedStack<LogContext>`) so bare logger calls auto-carry request context; `push_request_context(req)`
    + `set_workflow_fields(...)` (analogous to Bookkeeper's `set_task_fields`);
  - **logger name `uflowsd`**, JSON ISO-8601 → stderr, `--log-level` (Bookkeeper semantics);
  - **global middleware** wraps every request: `req = "<METHOD> <PATH>"`, elapsed, exactly one terminal
    `req-out` event (status + elapsed + `tag` on errors);
  - **workflow routes add context** `workflow_id` / `script` (+ `operation_id`/`operation` on dispatch);
  - stable grep-friendly event names: `uflowsd-startup-begin`, `uflowsd-listening`, `uflowsd-shutdown`,
    `workflow-submit-received`/`-completed`/`-rejected`, `workflow-resume-received`/`-completed`,
    `workflow-handler-failed`, `manifest-load-begin`/`-completed`, `manifest-reload-rejected`/`-reloaded`;
  - `/perf/*`-style synthetic endpoints skip request logging (like Bookkeeper);
  - replace the plain `console.println("microflows-service listening …")` with structured daemon logging.
- **Certified distribution is BLOCKED on the toolchain** — apps aren't signed today (trust-v1 cert is
  library-only; `drift deploy --app-dest` yields an unsigned native binary + provenance). Toolchain ask
  filed `2026-06-25T17:56:53Z-drift-workflows-release-notes.md`. We do NOT ship an unsigned daemon on the
  trusted path; we wait for app-cert. uflowsd is built + verified locally meanwhile, ready to certify.

## Implementation plan

1. **Rename `microflows-service` → `uflowsd`** — `microflows/runner/drift/{manifest,lock}.json`,
   `integration/coordinator-singular/{tools/emit_test_plan.py, justfile, test.py, perf.py}`,
   `microflows/{doc/*, examples/*}`, and the daemon's own strings in `microflows/runner/src/runner.drift`.
   Verify the integration still compiles + 165/165.
2. **`uflowsd.logging` module** (mirror Bookkeeper's resolver/scope/middleware shape).
3. **Wire logging into `service_main`** — global middleware, `--log-level`, workflow-context fields,
   the event vocab, `/perf/*` skip, replace `console.println`.
4. **Local verification** — `uflowsd --manifest … --port … --log-level info` emits startup/listening JSON;
   a submit emits route events + exactly one `req-out`; context fields auto-appear; error paths carry
   structured `tag`/`reason`; integration 165/165 stays green.
5. **Root-manifest entry (ready, not certified)** — add `uflowsd` (kind:app, 0.1.0, dep `microflows@0.2.0`)
   to root `drift/manifest.json`; once app-cert lands, mint claim + certify + post-cert review + announce.

## Files likely affected

- `microflows/runner/drift/{manifest,lock}.json`, `microflows/runner/src/runner.drift`
- `integration/coordinator-singular/{tools/emit_test_plan.py, justfile, test.py, perf.py}`
- `microflows/{doc/microflows_design.md, doc/roadmap.md, examples/manifest.json, examples/RUN_LOCAL.md}`
- New: `microflows/runner/src/logging.drift` (`uflowsd.logging`)
- Later: root `drift/manifest.json` (uflowsd app artifact)

## Verification criteria

- Integration `just test` 165/165 after the rename (uflowsd binary builds from source + drives the e2e).
- `uflowsd --manifest <m> --port <p> --log-level info`: startup/listening JSON on stderr; a workflow
  submit → route event(s) + exactly one terminal `req-out`; request-context fields auto-present; errors
  carry `tag`/`reason` (not prose); `/perf/*` skipped.
- (Blocked) cert deploy stages `uflowsd@0.1.0`; verify-then-run per the toolchain's app-cert command.

## Current status and next action

Status: **toolchain ask filed; starting the local build track.** Next: step 1 (rename
`microflows-service` → `uflowsd`) + re-verify the integration.

## Open questions / blockers

- **BLOCKER (toolchain):** app certification / verify-gated run path — filed; certified distribution waits.
- Pushcoin-facing daemon behaviors (HTTP surface details, manifest schema) — consult pushcoin via owner
  when a choice affects their flow (they're the critical user).

## Relevant review findings

- Apps deploy as runnable native binaries + same-repo app→lib ordering works, but apps are NOT certified
  (unsigned provenance only) → certified daemon needs the toolchain ask. (Spike `a3bb1c8a`.)
