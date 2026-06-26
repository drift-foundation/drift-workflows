# uflowsd — PROGRESS

See `README.md` (charter). Toolchain: staged driftc 0.33.61 / ABI 18 (app-cert complete).

## Status: DONE on staged 0.33.61 — app cert chain closed end-to-end (author → trust check → deploy → verify-app). Held for one clean commit; cert cut waits on the orchestrator promoting 0.33.61 + binding real evidence.

Naming settled: engine = `microflows` library (name unchanged); CLI = `microflows-runner` (one-shot driver
+ DB-free tooling: `--parse-check`/`--lower-source`/`--emit-content-hash`); daemon = **`uflowsd`** (renamed
from `microflows-service`, the `microflows.runner::service_main` HTTP front-door).

## Blocker (filed)

Apps aren't certified today (trust-v1 cert is library-only; `drift deploy --app-dest` → unsigned native
binary + provenance). Toolchain ask posted: `2026-06-25T17:56:53Z-drift-workflows-release-notes.md`
(certify + verify-gate `kind:app`). Certified `uflowsd` distribution waits on it; we build + verify locally.

## Checklist

- [x] **Rename `microflows-service` → `uflowsd`** (code): `microflows/runner/drift/{manifest,lock}.json`
      (artifact `uflowsd`, entry `microflows.runner::service_main`), `integration/coordinator-singular/
      {tools/emit_test_plan.py, justfile, test.py, perf.py}`, daemon strings in `runner.drift`. Lock
      re-prepared (artifacts: microflows-runner + uflowsd). No worker_id mismatch (tests use own ids).
- [x] Rename docs/template refs: `microflows/examples/{manifest.json,RUN_LOCAL.md}`, `microflows/doc/{microflows_design,roadmap}.md`. Full sweep clean (only work/ notes mention the old name).
- [x] Verify gate green after rename: **`just test` → integration 165/165, 0 failures**. Daemon builds + drives the e2e as `uflowsd`.
- [x] `uflowsd.logging` module (`microflows/runner/src/logging.drift`) — Bookkeeper-mirrored
      `AppLogResolver`/`ContextResolver` over VT-scoped `rt.thread_registry` + `ScopedStack<LogContext>`;
      `ensure_request_context`/`push_request_context`/`set_workflow_fields`.
- [x] Wire logging into `service_main`/`_service_run`: logger `"uflowsd"` JSON ISO-8601→stderr, `--log-level`
      (debug|info|error), `AppLogResolver` registered; global `add_middleware` (one terminal `req-out`,
      status/elapsed/`tag`, `/perf/*` skip); full event vocab; `console.println`→structured events.
      (Fix: `std.text as text` collided with a local binding → aliased `txt`.)
- [x] Local verify — **GREEN**: runner compiles; `uflowsd` binary builds (entry `service_main`);
      **integration `just test` 165/165**; live-DB log-capture smoke shows every event as JSON ISO-8601,
      `workflow_id`/`script`/`req` auto-attached via the resolver (incl. `req-out`), absent on
      `/healthz`·`/readyz`, exactly one `req-out` per request, structured 400 outcome. All acceptance
      criteria satisfied.
- [x] **App-cert SHIPPED in 0.33.58** → `uflowsd@0.1.0` (kind:app) added to ROOT `drift/manifest.json`.
      See the "0.33.58 adoption" section below — prepare/build/author/verify/cert are blocked on the dep
      re-stage, not on the toolchain anymore.

## Logging spec (Bookkeeper-mirrored) — for the implementation step

- `uflowsd.logging`: `AppLogResolver`-style `log.ContextResolver` over `rt.thread_registry()` +
  `ScopedStack<LogContext>`; `push_request_context(req)` (ScopeGuard) + `set_workflow_fields(workflow_id,
  script, …)` (mutates top scope so the post-handler `req-out` carries them). Logger name `uflowsd`,
  JSON ISO-8601 → stderr, `--log-level` (Bookkeeper semantics).
- Global middleware: `req = "<METHOD> <PATH>"`, elapsed, exactly one terminal `req-out` (status/elapsed/
  `tag` on error). `/perf/*` skip.
- Event vocab (stable, grep-friendly): `uflowsd-startup-begin`, `uflowsd-listening`, `uflowsd-shutdown`,
  `workflow-submit-received`/`-completed`/`-rejected`, `workflow-resume-received`/`-completed`,
  `workflow-handler-failed`, `manifest-load-begin`/`-completed`, `manifest-reload-rejected`/`-reloaded`.
- Reference: `/home/sl/src/pushcoin/bookkeeper/src/{logging.drift, app.drift:41, routes/submit.drift:18}`.

## Notes

- The CLI (`microflows-runner`) is unchanged — it's the one-shot driver our integration/stress/parser gates
  use; pushcoin's ask is the daemon only.

## 0.33.58 adoption + pool re-cert (in progress — BLOCKED on dep re-stage)

Toolchain **0.33.58** (git 8fe6a7f4) staged at `~/opt/drift/staged/toolchain/drift-0.33.58+abi18`. It brings
certified runnable **app** artifacts + `drift verify-app` (uflowsd unblocked), a trust-vocab change
(kinds = `package`|`app`; `library` deprecated alias), and a **forced pool-wide re-cert** (SCI now hashes
artifact `kind`; author/cert claims → schema v2, provenance → v4; v1/v3 rejected). Ref
`2026-06-26T12:39:14Z-drift-lang-release-notes.md`.

**Source changes DONE (staged-ready):**
- `kind: library` → `kind: package` in root + `singular/drift` + `microflows/drift` manifests.
- Minor bumps (downstream compat affected by SCI/claim-schema change): **singular 0.6.0→0.7.0,
  microflows 0.2.0→0.3.0**; **uflowsd 0.1.0** added to ROOT `drift/manifest.json` as `kind: app`
  (entry `microflows.runner::service_main`, modules = runner src, deps microflows@0.3 + web-client/web-rest/
  mariadb-rpc). `just author-claim` recipe extended to mint `uflowsd` too.

**BLOCKED — cannot prepare/build/author/verify/cert yet (deps not re-staged):**
`~/opt/drift/staged/libs` is mid-re-cert: web-* are staged at NEW v2 versions (web-rest 0.6.0,
web-client 0.5.0, web-jwt 0.5.0, net-tls 0.6.0) but **`mariadb-rpc 0.7` and `mariadb-wire-proto 0.5`
(our deps) are ABSENT** (staged tops at 0.5.2 / 0.3.3, v1 claims). `drift prepare` fails:
`package dependency 'mariadb-rpc 0.7' not satisfied`. This is the "cert blocked until all deps updated".

**Pending when our deps land (mariadb-rpc + wire-proto at 0.33.58 v2):**
1. Update our `package_deps` version constraints to the new 0.33.58 dep versions (e.g. web-rest 0.5→0.6,
   web-client 0.4→0.5, web-jwt 0.4→0.5; mariadb-rpc TBD — final version unknown until staged).
2. `just prepare` (root + singular/drift + microflows/drift + runner + stub) on 0.33.58.
3. Decide uflowsd dep model under the root deploy: Model A (dep microflows@0.3 package, current) vs Model B
   (source-compile microflows into uflowsd) — verify the root deploy resolves the intra-manifest app→lib dep.
4. Build + test singular/microflows/integration/uflowsd on 0.33.58.
5. `just reseal` → v2 author-claims (+ uflowsd); `drift verify-package` (singular/microflows) + `drift verify-app` (uflowsd).
6. Stage + cert (when the whole pool's deps are ready). Then post-cert review + announce + pushcoin reply.

## 0.33.58 re-cert — UNBLOCKED on deps; packages done; app author-claim is the last question

Deps re-staged (mariadb-rpc 0.8, mariadb-wire-proto 0.6, web-rest 0.6, web-client 0.5, web-jwt 0.5,
net-tls 0.6). Updated all `package_deps` constraints accordingly; re-prepared every lock on 0.33.58.
**Full suite GREEN on staged 0.33.58: singular 16, microflows 20, integration 165/165** — everything
compiles against the bumped deps; uflowsd binary builds; root manifest app→lib dep (uflowsd→microflows@0.3)
resolves (Model A confirmed).

**v2 author-claims minted (packages):** `singular 0.7.0` (SCI f600…) + `microflows 0.3.0` (SCI 94db…),
schema_v=2, kind=package. `drift prepare` + (pending) trust-check.

**BLOCKED — only `uflowsd` app author-claim:** both `drift author` (binary) and `tools.drift_author publish`
filter to `kind == "package"` and reject the app (cli.py:120; stale comment "apps aren't verified through
the consumer closure path"). The v2 schema + `drift verify-app` three-leg (author==cert==provenance) expect
an app author-claim, but there's no minting surface. **Question posted:
`2026-06-26T14:51:42Z-drift-workflows-release-notes.md`** — relax filter to `("package","app")`, or where is
the app author leg minted? When answered → mint uflowsd author-claim → `just deploy` →
`drift verify-package` (singular/microflows) + `drift verify-app` (uflowsd) → stage for cert.

NOTE: re-author/claims are uncommitted; manifest is final on our side EXCEPT whatever the app-author answer
implies. Hold the commit until uflowsd's author path is resolved (one cut).

## App-cert chain CLOSED — toolchain shipped the three missing producer/verifier surfaces (0.33.59 → 0.33.61)

The app-author question triggered a run of toolchain fixes; app-cert had shipped surface-by-surface and each
release relaxed one stale `kind=="package"` guard the workflows team surfaced:
- **0.33.59** — `drift author` minted app author-claims (`_AUTHORABLE_KINDS=("package","app")`).
- **0.33.60** — `drift trust check/bootstrap` validated the app author leg (`trust.py` had its own package-only
  filter at lines 423/663; orphan-claim false positive gone) + manifest `kind` is now strictly `package|app`.
- **0.33.61** — `drift deploy` finally emits the app **author + cert legs** (`drift_deploy.py:2117` gate +
  the `:2352-2360` "apps don't produce a cert claim" `pass` were the last gap); cert-claim `artifact_path`
  now names the binary, not `<id>.zdmp`.

**Local trust store changes (drift/trust.json, both roles, same Foundation kid 6DSI…):**
- `microflows.runner.*` — the app's own namespace (new artifact; exact-match, not prefix-covered by
  `microflows.*`).
- `net_tls.*`, `web.client.*`, `web.jwt.*`, `web.rest.*` — web-stack deps the app pulls in (the packages only
  dep mariadb, so these were never granted before). All Foundation-authored, same kid.

**GREEN on staged 0.33.61 (`git e5eaa46f`):**
- `just author-claim` mints all three (singular 0.7.0, microflows 0.3.0, uflowsd 0.1.0 `artifact_kind=app`).
- `just prepare` resolves 3 artifacts (uflowsd → microflows@0.3 + web-stack + mariadb; Model A).
- `just trust-check` → `✓ singular ✓ microflows ✓ uflowsd`.
- `just deploy --app-dest build/deploy-app` publishes all three; app dir carries
  `uflowsd{,.author-claim,.author-profile,.cert-claim.<kid>.json,.provenance.zst}`.
- `drift verify-app build/deploy-app/uflowsd/0.1.0` → OK [app], three legs + provenance match.
- `drift verify-package` singular/microflows → OK, provenance matches.
  (dev/no-evidence sentinel warnings expected; orchestrator binds real evidence at the cert cut.)

**Uncommitted, held for one clean cut.** Adds `drift/uflowsd.author-claim` + `drift/uflowsd.author-pubkey.b64`
(untracked) and a clean one-block-per-namespace `drift/trust.json` diff (microflows.runner + 4 web-stack).
Remaining: orchestrator promotes 0.33.61 from staged → certified, re-runs deploy with real cert-suite evidence,
stages for cert. Then post-cert review + announce + pushcoin reply.
