# Workflow-Failpoint Resilience — Charter

> **`DESIGN.md` is the current source of truth** for the mechanics, the fix pattern, and the
> per-boundary results. This charter records intent, scope, and status; where it differs, `DESIGN.md`
> (and `PROGRESS.md` for day-to-day detail) wins.

## Short-Term Objective

Validate that the certified workflow-composition MVP (`singular`, `microflows`, `uflowsd`) actually
survives the one class of fault its own `RpcCommitError.kind` handling exists for — a COMMIT whose
acknowledgement is lost (`AmbiguousWrite`) or never sent (`NotSent`) — by inducing it for real via
`drift-mariadb-client`'s `mariadb-failpoint-proxy`, not just trusting that the code matches the
documented `kind` contract in theory.

## Current Behavior / Problem

Found via fault injection (not theoretical): `HostException(BackendUnavailable)` from an
AmbiguousWrite commit crashed `mfrunner` (`runner: fatal`) at 5 dispatch-path SP calls instead of
returning a clean retriable outcome and releasing the lease promptly. The same class of gap existed
in Singular's own gateway (masked there by `web-rest`'s generic 500 catch-all — no process crash, but
`AmbiguousWrite`/`ServerRejected` were collapsed into the same undifferentiated response, discarding
the distinction `RpcCommitError.kind` exists to preserve).

## Accepted Design Decisions

- Fix the general pattern once (a "_checked" errors-as-values chain, mirrored identically in both
  `microflows/host.drift` and `singular/gateway.drift`) and apply it to every identified boundary in
  one pass — not just the boundaries one consumer team happens to prioritize (explicit user decision
  after app-team feedback asked to prioritize participant-dispatch over composition).
- `_defer` (generic `host.release`), not `_defer_dispatch` (`sp_mf_operation_dispatch_defer`, which
  has its own `varchar(64)` `arg_reason` column), is the safe choice for a shared retry helper whose
  reason strings are built from an unbounded site name.
- A malformed response document (`_str_req`/`_parse_object_doc`) is a data-integrity bug, not a
  transient network condition — intentionally still throws; the checked chain covers connection/commit
  failures only, not the full throw surface.
- `sp_singular_fail` gets the same `_core` split for consistency but is NOT separately pinned: no
  reachable caller exists in any current production host/gateway pattern (grepped the whole repo).

## Concrete Implementation Plan

1. ~~Baseline (no proxy): confirm existing coverage already proves clean complete/reverse.~~ Done.
2. ~~Wire MariaDB traffic through the proxy for the domain under test.~~ Done.
3. ~~Inject `AmbiguousWrite` on each targeted COMMIT, one at a time (5 microflows + 3 singular
   boundaries).~~ Done — see `PROGRESS.md` for per-boundary results.
4. ~~Fix each finding via the shared `_checked` pattern; pin a test per boundary.~~ Done — 81
   assertions across two new test files.
5. ~~Wire the new pinned tests into an actual automated gate.~~ Done — new root-level `just
   test-resilience` (see `justfile`) owns the proxy lifecycle end to end; 81/81 across 2 consecutive
   runs, `EXIT_CODE=0`.

## Files Likely Affected

- `microflows/packages/microflows/src/host.drift`, `microflows/runner/src/runner.drift` (done)
- `singular/drift/packages/singular/src/gateway.drift`, `microflows/participant-stub/src/app.drift` (done)
- `microflows/db-tests/failpoint_resilience_test.py`, `microflows/participant-stub/tests/http/failpoint_resilience_test.py` (done, gated)
- root `justfile` (new `test-resilience`/`_test-resilience-locked` recipes) — done

## Verification Criteria

- No `runner: fatal` / process crash from any of the 8 targeted AmbiguousWrite commits. ✅
- `RpcCommitError.kind` respected end to end (retriable path vs rejected, never collapsed). ✅
- Lease/work-item state never worse than the durable write implies; retry/reclaim converges with the
  same identity; no duplicate mutation beyond idempotent replay. ✅
- Every target's failpoint provably fires (`assert_all_fired`) — no vacuous passes. ✅
- Full monorepo `just test` (root, combined) stays green: no regression from the fix. ✅ (61/61, 328.8s)
- The 81 pinned assertions run automatically as part of a canonical gate the proxy lifecycle owns end
  to end (build/start/stop), not as manually-invoked scripts requiring an externally-running proxy. ✅
  (`just test-resilience`, 2 consecutive runs, 81/81, `EXIT_CODE=0`)

## Current Status And Next Action

Status: **DONE.** Fix landed and verified (8/8 boundaries, 0 regressions); new `just test-resilience`
root gate owns the proxy lifecycle end to end and runs both pinned test files (81/81, 2 consecutive
runs, `EXIT_CODE=0`); `test`/`test-singular`/`test-microflows` confirmed byte-identical before/after.

Next action: none outstanding for this effort. The Singular test file remains untracked in git by the
user's own choice (declined staging when asked — git ops are explicit-permission-only here).

## Open Questions

None outstanding. (Resolved: separate `just test-resilience` gate, not folded into `_test-combined` —
cross-repo proxy dependency + materially slower than the cert-critical path. Proxy discovery is
`MARIADB_FAILPOINT_PROXY_BIN` env var, falling back to the sibling `drift-mariadb-client` checkout's
default build path.)

## Relevant Review Findings

**Round 1** (test-file coverage vs canonical regression):
1. **New failpoint tests are not wired into any canonical gate.** Root `just test` passes without
   running either new file — the reported "61 ok" does not prove the 81 failpoint assertions. Fixed:
   new `just test-resilience` gate.
2. **The Singular failpoint test file was untracked in git** (`??`). Left to the user to stage
   (git operations are explicit-permission-only in this repo).
3. **Both new tests currently require an externally-running proxy.** Fixed: the gate now owns the
   proxy's full lifecycle.

**Round 2** (gate correctness + doc staleness):
4. **`test-resilience` used `exec` for its flocker call, skipping the DB-teardown EXIT trap.** `exec`
   replaces the shell process image, so a trap registered earlier in that same script never fires.
   Fixed: removed `exec`, runs flocker as a normal foreground command (verified by bringing the DB
   fully down, re-running the gate, and confirming it returns to `absent` afterward).
5. **Two files still untracked**: the Singular test file (round 1) plus this `README.md` itself.
   Still left to the user — same explicit-permission-only reason.
6. **This README had stale "not yet gated" / open-question wording** after the gate landed. Fixed —
   see the plan/files/open-questions sections above.
