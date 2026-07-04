# Release — AmbiguousWrite resilience (singular 0.9.0 · microflows 0.7.0 · uflowsd 0.6.0)

> **Status: DRAFT — not yet submitted to `build-orchestrator`.** This is a source-level fix +
> pinned-regression release, not a new feature. It builds on the already-certified workflow
> composition MVP (`work/workflow-composition/RELEASE_ANNOUNCEMENT_DRAFT.md`, singular 0.8.0 ·
> microflows 0.6.0 · uflowsd 0.5.0, CERTIFIED by run `20260703-174026-drift-lang-5c6e03f`) — no schema
> changes, no participant-facing contract changes, no composition behavior changes. Full day-to-day
> story: `work/workflows-failpoint-resilience/README.md` (charter) and `PROGRESS.md`.

## Version audit

| Artifact | Was (last certified) | Now (`drift/manifest.json`) | Status |
|---|---|---|---|
| `singular` (package) | 0.8.0 | **0.9.0** | real source fix (`gateway.drift` — see below) |
| `microflows` (package) | 0.6.0 | **0.7.0** | real source fix (`host.drift`/`runner.drift` — see below) |
| `uflowsd` (app) | 0.5.0 | **0.6.0** | its own `entry_module` is `microflows/runner/src/runner.drift`, directly touched by this fix (dep on `microflows` bumped `0.6`→`0.7`) |

`mfrunner` (one-shot CLI) and `microflows-participant-stub` remain component-local dev artifacts
(`0.0.0`) — unchanged convention.

**Why a minor bump, not a patch:** both `singular`'s `SingularGateway` and `microflows`'s
`MicroflowsHost` interfaces gained new public methods (`start_checked`/`resume_checked`/
`complete_checked`/`fail_checked`; `call_submit_checked`/`operation_request_checked`/
`operation_settle_checked`/`checkpoint_reverse_child_reopen_checked`/
`checkpoint_reverse_child_settle_checked`) — an additive public API surface change, matching this
repo's own pre-1.0 convention of a minor bump per release, not a patch-only fix.

## What's in this draft

### The defect

Neither `singular`'s nor `microflows`'s host/gateway layer had ever been validated against a COMMIT
whose acknowledgement is lost (`RpcCommitError::AmbiguousWrite`) or never sent (`::NotSent`) — the
one class of fault their own `kind` handling exists for. Fault-injected for real via
`drift-mariadb-client`'s `mariadb-failpoint-proxy` (one-shot ack-loss, real MariaDB, not a mock).
Found: `mfrunner` crashed outright (`runner: fatal`, exit 1) at 5 dispatch-path SP calls instead of
returning a clean retriable outcome and releasing its lease promptly. Singular's own gateway had the
same class of gap, masked by `web-rest`'s generic per-request 500 catch-all (no process crash there,
but `AmbiguousWrite`/`ServerRejected` were collapsed into the same undifferentiated response,
discarding the distinction `RpcCommitError.kind` exists to preserve).

### The fix

- **New "_checked" errors-as-values chain**, mirrored identically in `microflows/host.drift` and
  `singular/gateway.drift`: every low-level throwing helper in the SP-call path gets a sibling
  returning `core.Result<T, HostErrorKind|RuntimeErrorKind>` instead of throwing, so a transient
  backend condition is recoverable as a plain value (a thrown error's non-scalar `kind` field can't be
  projected through a typed catch in this toolchain — see `PROGRESS.md` for the full mechanism). Every
  existing throwing public method becomes a one-line wrapper around the new `_core` logic — no
  existing caller changes behavior.
- **microflows/runner.drift**: all 5 dispatch call sites (`call_submit`, `operation_request`,
  `operation_settle`, `checkpoint_reverse_child_reopen`, `checkpoint_reverse_child_settle`) now catch
  the retriable kind and route it through the existing short-defer-and-release-lease path
  (`_defer`/`host.release`) instead of crashing — a clean `Outcome::Deferred`/`DeferFailed` response,
  lease released/deferred within `DISPATCH_DEFER_SECONDS` (5s) instead of held for the full lease TTL
  (30s).
- **microflows/participant-stub** (the Singular-backed reference participant): all 4 `handle_put` call
  sites (`start`, `complete`, `resume`, the reclaim-path `complete`) now respond `202 {"state":
  "pending"}` on a retriable kind — the same response this file already uses for every other "try
  again" condition — instead of falling through to an undifferentiated 500.
- **New `just test-resilience` gate** (root `justfile`): owns the full `mariadb-failpoint-proxy`
  lifecycle (discovery, build-binary steps, start, readiness probe, `trap`-based teardown regardless
  of pass/fail) and runs both new pinned test files. Deliberately kept separate from `test`/
  `_test-combined` — the proxy binary comes from a sibling `drift-mariadb-client` checkout that is not
  a declared cert capability, and this gate is materially slower than the cert-critical path.
  `test`/`test-singular`/`test-microflows`/`test-integration` are unchanged (verified byte-identical).

### Explicitly out of scope for this release

- **`sp_singular_fail`** got the same `_core`/`*_checked` split for consistency, but is **not**
  separately pinned: grepped the whole repo, no reachable production caller exists in any current
  host/gateway pattern (only Singular's own e2e/stress/perf test files call `.fail()` directly).

## Migration

None. No schema/table/column changes — this is a pure runtime/host-layer fix.

## Breaking changes

None. No participant-facing HTTP contract change, no composition behavior change, no schema change.
Every existing throwing public method (`call_submit`, `start`, `resume`, etc.) keeps its exact
existing signature and behavior — the new `_checked` methods are additive.

## Verification

- 8 targeted `AmbiguousWrite` fault-injections (5 microflows dispatch boundaries + 3 Singular gateway
  boundaries: `start`/`complete`/`resume`), each with a ground-truth commit ordinal (via
  `mysql.general_log`, never guessed) and an `assert_all_fired` check (closes a real vacuous-pass gap
  found mid-effort — see `PROGRESS.md`).
- **81 pinned assertions** (61 `microflows/db-tests/failpoint_resilience_test.py` + 20
  `microflows/participant-stub/tests/http/failpoint_resilience_test.py`), run via the new
  `just test-resilience` gate: 2 consecutive full runs, 81/81 passed, `EXIT_CODE=0` both times.
- Root `just test` (singular → microflows → coordinator-singular integration, one combined plan):
  **61 ok, 0 failed, 328.8s** — no regression from this fix anywhere in the monorepo.
- Component-level: `microflows`'s own `just test` — 25/25 (unit/e2e) + 156/156 (`sp_operation`) +
  131/131 (`sp_call`) + 50/50 (`call_integration`), exit 0.

Full per-boundary results, the two non-obvious sub-findings hit along the way (a Drift typed-catch
projection limit and a `varchar(64)` column overflow root-caused via debug instrumentation, not
guessed), and the review rounds this draft answers: `work/workflows-failpoint-resilience/PROGRESS.md`.

## Certification

**Not yet submitted.** `drift/manifest.json` reflects the proposed versions above; `drift/lock.json`
and `drift/*.author-claim` need `just reseal` (author-claim + prepare + trust-check) before this is
ready for a `build-orchestrator` submission.
