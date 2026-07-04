# Workflow composition resilience validation — Progress

## Status

Just starting. See `DESIGN.md` for the full plan and mechanics. Current step: baseline (step 1) —
confirming existing coverage already proves "small composed workflow completes and reverses cleanly, no
proxy" before building anything new.

## Step 1 — baseline (no proxy) — DONE, no new code needed

`microflows/db-tests/call_integration_test.py`'s `completes`/`reverse_child`/`nested_abc` scenarios and
`integration/coordinator-singular/test.py`'s `ex_composition_*` checks already drive a real
parent-calls-child workflow through submit → child completes → (later) parent reversal → child
compensates → parent settles, against a real MariaDB, no proxy. Both are part of the currently-certified
suite (61/61 green). Baseline is the certified suite itself, not a new artifact.

## Step 2 — wire MariaDB traffic through the proxy — DONE

Built `mariadb-failpoint-proxy` locally from `drift-mariadb-client` (`just build-app
mariadb-failpoint-proxy`, using the certified `driftc 0.33.68|abi19` toolchain). Started it as a real
subprocess: `--data-port 43306`, `--backend-host/port 127.0.0.1:34214` (our dev MariaDB), `--control-port
43307`. Health check confirms `ready:true`.

**Passthrough proof:** ran the *entire* `call_integration_test.py` suite (fresh schema, freshly-built
`mfrunner`) with `DB_HOST=127.0.0.1 DB_PORT=43306` (the proxy's data port, instead of `34214` direct) and
nothing armed — **50/50 passed**, identical to a direct connection. Cross-checked against the proxy's own
JSON-Lines log: 68 `client_accept`/`backend_connect` pairs, real `commit_observed` events, all forwarding
to `127.0.0.1:34214` — confirms the traffic genuinely routed through the proxy rather than silently
bypassing it.

**The knob:** `mfrunner`'s DB connection isn't env-configured — it's read from the manifest JSON
(`deployment.db.host`/`deployment.db.port`), written by `call_integration_test.py`'s own
`write_manifest()` from its module-level `HOST`/`PORT` globals (which *are* env-configured). So pointing
the whole existing test at the proxy needed zero code changes, just `DB_PORT=43306` in the environment.
For the real fault-injection cases (step 3), the out-of-band `pymysql` assertion reads in the same file
also currently share that same `PORT` global — will need to split that so assertions read the real DB
directly while only the manifest's `db.port` is proxied, so an armed failpoint can't accidentally affect
the harness's own verification queries.

## Step 3a — first case: AmbiguousWrite on sp_mf_call_submit's COMMIT (in progress)

**Determined the exact commit ordinal, from ground truth (MariaDB `general_log`, not guessed):** a
single fresh "submit parent" `mfrunner` invocation (workflow-id + script + arguments, parent's first
step is `call child@1.0.0`) issues exactly 12 commits, one per SP call, in this fixed order:

```
1. sp_mf_plan_get
2. sp_mf_clock_read(30)
3. sp_mf_workflow_create_planned
4. sp_mf_clock_read(30)
5. sp_mf_workflow_claim_by_id
6. sp_mf_args_get
7. sp_mf_operation_result
8. sp_mf_clock_read(30)
9. sp_mf_call_submit        <-- TARGET: arm `match.nth = 9`
10. sp_mf_call_inspect
11. sp_mf_clock_read(1)
12. sp_mf_workflow_release
```

Cross-checked against the proxy's own log for the same isolated run: exactly 12 `commit_observed`
events on one connection, matching the SQL-log count exactly.

Next: arm `{"op":"arm","match":{"nth":9},"action":"drop_server_response_after_forward"}`, drive the same
submit, and check resulting DB state (does the child workflow row + call operation exist exactly once,
does the parent end up retriable rather than stuck) rather than asserting on `RpcCommitErrorKind`
directly (already covered by `host.drift`).

### Result: FINDING — recovery is correct, but the first-attempt experience is degraded

**What happened.** Armed nth=9, drove a fresh "submit parent" through the proxy. The write landed
durably (MariaDB itself committed — confirmed via direct query: parent workflow row, call row, and
child workflow row all present and correctly linked) but the client-side ack was dropped, exactly the
documented `AmbiguousWrite` shape. `mfrunner`'s own process, however, **crashed**: `returncode=1`,
stderr `runner: fatal`, no JSON output at all — not the clean `{"workflow":"pending",...}` a caller
would need to know "try again." Reproduced twice (two separate armed runs), same crash both times.

**Why recovery still holds.** `sp_mf_workflow_release` (commit #12) never ran — the crash happened at
#9, before the workflow's lease was released — so the workflow sat holding its own lease for the
remainder of its ~30s duration. Once `lease_expires_at` passed, a plain resume (same `--workflow-id`,
no `--script`) succeeded cleanly: `{"workflow":"pending","reason":"operation_pending",...,
"child_workflow_id":"<same id as before>"}` — the *same* child id as the pre-crash row, proving the
retry correctly discovered the already-created child via `sp_mf_call_submit`'s own idempotent-replay
path rather than creating a duplicate. Confirmed via direct query: exactly 2 workflow rows (parent +
child), exactly 1 call row, no duplicates. `mfinspect inspect <parent_id>` renders a coherent tree —
the parent's one `calls` entry recursing correctly into the child's own state.

**The actual gap:** not a data-correctness bug, an *operational* one. Whatever dispatch code in
`runner.drift` calls `sp_mf_call_submit` (indirectly, through `host.drift`'s `_finish_stmt_and_commit`
→ throws `HostException(kind=BackendUnavailable)` on `AmbiguousWrite`/`NotSent`) doesn't appear to
catch that exception at this call site the way other retriable conditions elsewhere in the dispatch
loop do — it propagates all the way to `main()`'s top-level `catch unexpected`, producing an
uninformative crash instead of a clean "pending, retry sooner" response. Practical consequence for a
real caller: no structured signal to retry on, and even a blind retry has to wait out the full lease
duration (~30s here) rather than the shorter `defer_until` window a graceful handler would set.

**Against the user's verification criteria:**
- no duplicate child workflow creation — **holds**
- no duplicate participant execution beyond idempotent replay — n/a at this boundary (no participant
  call has happened yet at `call_submit` time)
- no stuck parent except expected pending/retry — **degraded, not broken**: recovers, but only after
  the full lease timeout, not immediately, and the first-attempt caller sees a crash, not a "pending"
  signal
- terminal replay remains stable — not yet exercised at this boundary (workflow isn't terminal)
- `mfinspect` can explain the tree after recovery — **holds**

## Full-scope fix + pinned coverage (all 5 identified boundaries)

Per explicit user direction: do not defer the `call_submit` finding, and do not let app-team
feedback (prioritizing `sp_mf_operation_request`/`sp_mf_operation_settle`, the participant-dispatch
path) shrink composition-resilience scope. Fix the general pattern once, apply it to every
identified boundary, pin a test per boundary, record anything left uncovered explicitly.

### The fix (see DESIGN.md's "The fix pattern" for full detail)

1. **host.drift**: new "_checked" siblings of every low-level throwing helper in the SP-call chain
   (`_acquire_conn_checked`, `_exec_with_conn_checked`, `_next_checked`, `_finish_stmt_and_commit_checked`,
   `_read_result_doc_checked`, `_call_sp_doc_checked`) — each returns `core.Result<T, HostErrorKind>`
   instead of throwing. A `_core` version of each of the 5 target host methods, built on this checked
   chain, exposed via 5 new interface methods (`call_submit_checked`, `operation_request_checked`,
   `operation_settle_checked`, `checkpoint_reverse_child_reopen_checked`,
   `checkpoint_reverse_child_settle_checked`). The existing throwing public methods are now one-line
   wrappers around their `_core` counterpart — every other existing caller is unaffected.
   - Found along the way: the *existing* `_core`/`*_best_effort` split (`_call_hint_refresh_core`,
     `_child_terminal_notify_core`) only catches `managed:ManagedError` — it does NOT catch the
     `HostException` thrown directly by `_finish_stmt_and_commit`/`_exec_with_conn`/`_next`, so even
     that established pattern didn't close this gap on its own. Deliberately narrower than full
     fidelity: `_str_req`/`_parse_object_doc`'s malformed-data paths still throw (a data-integrity bug,
     not a transient network condition).
2. **runner.drift**: shared helper `_defer_on_backend_unavailable` at all 5 dispatch call sites —
   matches `BackendUnavailable`/`BackendTimeout` from the checked Result and routes it through `_defer`
   (a short, `DISPATCH_DEFER_SECONDS`=5s defer via `host.release`); anything else re-throws unchanged.
   - **First attempt used `_defer_dispatch` (host.defer_dispatch / `sp_mf_operation_dispatch_defer`)
     and this was WRONG for `checkpoint_reverse_child_reopen`**: calling it as this error path's
     follow-up produced a genuine NEW `runner: fatal` crash, caught by the pinned test itself (see
     below) before this shipped. **Root-caused via temporary debug instrumentation** (a try/catch
     printing `e.encode_compact()` around the call, plus a print of the raw server error message at
     `_server_err_kind` — `encode_compact()` alone doesn't surface a non-scalar field like
     `HostErrorKind::BackendRejected`'s `detail`, the same typed-catch scalar-projection limit as
     everywhere else in this codebase): the actual MariaDB error was `"Data too long for column
     'arg_reason' at row 0"`. `sp_mf_operation_dispatch_defer`'s `arg_reason` column is `varchar(64)`,
     and the constructed reason string for this site —
     `"checkpoint_reverse_child_reopen:backend_unavailable:wire-read-eof"` — is 66 characters, over the
     limit. Not a "reversal direction is unsafe" issue at all (`_defer_dispatch` is used successfully
     for other reversal-direction conditions elsewhere in runner.drift with shorter reason strings, e.g.
     `"compensation_pending"`). Fixed by switching to `_defer` (`host.release`): its underlying SP
     doesn't take a reason column at all (the reason is a client-side `Outcome` field only, never
     persisted), so it has no such length constraint by construction — the right choice for a shared
     helper whose reason strings are built by concatenating a site name of unbounded length. Re-verified
     clean across repeated pinned-test runs after the fix.

### Pinned tests: `microflows/db-tests/failpoint_resilience_test.py` (new)

One target function per boundary, each arming `mariadb-failpoint-proxy`'s one-shot ack-loss failpoint
on the exact ground-truth commit ordinal (determined via `mysql.general_log`, never guessed) and
asserting: no `runner: fatal`, a clean structured JSON outcome, the failpoint genuinely fired
(`assert_all_fired` — closes a real vacuous-pass gap this session hit: the proxy's `arm` op requires a
`label` field, and an early draft of the harness omitted it, silently no-op'ing every arm call until
this check caught it), the lease is released/deferred promptly (not held for the full `LEASE_SECONDS`
TTL), and a retry/resume converges correctly with no duplicates.

| Target | Commit ordinal (of N) | Scenario | Result |
|---|---|---|---|
| `sp_mf_call_submit` | 9 of 12 | fresh "submit parent" (composition) | Deferred; retry same `child_workflow_id`, 1 call row |
| `sp_mf_operation_request` | 10 of 14 | standalone single-op workflow | Deferred; retry idempotent, 1 operation row |
| `sp_mf_operation_settle` | 12 of 14 | standalone single-op workflow (final settle) | DeferFailed(`release_fence_lost`) — CORRECT, see below |
| `sp_mf_checkpoint_reverse_child_reopen` | 16 of 19 | reverse_child, 3rd resume (T1 reopen) | Deferred; retry re-reopens (AlreadyReopened), converges to failed/compensated |
| `sp_mf_checkpoint_reverse_child_settle` | 10 of 12 | reverse_child, 5th resume (final settle) | DeferFailed(`release_fence_lost`) — CORRECT, see below |

**A real, benign nuance found on the two FINAL-settle targets** (`operation_settle` on a single-op
workflow's `is_final=true` settle; `checkpoint_reverse_child_settle` on the parent's last checkpoint
before its own terminal `reversed(5)`): the underlying SP write is ALSO the write that completes/
terminates the workflow. When its commit ack is lost, the write is still durable — the workflow
reaches its terminal state before `_defer_on_backend_unavailable`'s own follow-up `_defer` call runs,
so that follow-up legitimately sees a stale `fencing_token` and gets `FenceLost` ->
`Outcome::DeferFailed(reason="release_fence_lost")`. Verified via direct DB read: `lease_owner` is
NULL and `state` is already the terminal value at that point — "prompt release" happened via the
settle's own commit, not via the defer. Not a bug; the pinned tests assert this exact shape (accepting
either `Deferred` or this specific `DeferFailed` as correct, and confirming terminal DB state) rather
than papering over it.

**Full result, 3 consecutive runs:** 61/61 assertions pass across all 5 targets, stable (no flakiness).

### Status: full-scope pass COMPLETE for all 5 originally-identified boundaries

Composition (`call_submit`, `checkpoint_reverse_child_reopen`, `checkpoint_reverse_child_settle`) and
participant-dispatch (`operation_request`, `operation_settle`, the app-team's own priority) are both
fixed and pinned — no boundary was skipped or deferred.

**Full `just test` regression after the fix: 61/61 ok, 0 failed, 321.5s.** No other suite regressed —
the new "_checked" host.drift helpers and the 5 updated runner.drift dispatch sites are additive
(every existing throwing caller is untouched), and the full existing gate (build + unit + live SP
regressions + coordinator-singular integration) confirms it.

## Singular claim/start/complete increment (2026-07-04, follow-up)

The Microflows pass above left Singular's own claim/start/complete boundary uncovered. Per explicit
user direction, that gap is closed in the same release rather than left open: "Since this release is
specifically about AmbiguousWrite resilience, that gap needs to be either fixed and pinned now or
proven out of scope by a concrete reason. Prefer fixing/pinning now."

### Survey

`singular/drift/packages/singular/src/gateway.drift` is an exact structural mirror of microflows'
`host.drift` — same `SingularException { kind: RuntimeErrorKind }` shape, same
`_finish_stmt_and_commit` classifying `RpcCommitErrorKind` (`AmbiguousWrite`/`NotSent` ->
`BackendUnavailable`, `ServerRejected` -> `BackendRejected`), same uncaught-throw gap. Its production
caller is `microflows/participant-stub/src/app.drift` (`handle_put`'s `start()`/`complete()`/
`resume()` calls) — the PushCoin-participant reference architecture. One material difference from
mfrunner's crash surface: `participant-stub` is a long-running REST server via `web.rest`, and
`web-rest`'s own framework (`drift-web/packages/web-rest/src/app.drift:326`) ALREADY has a generic
`catch { ... 500 "unhandled exception" ... }` around every request handler — so there is no
process-level "runner: fatal" equivalent for Singular; an ambiguous-write commit already produced a
clean per-request 500, not a crash. The real gap was narrower but still real: that generic 500
collapses `AmbiguousWrite`/`NotSent` (retriable) and `ServerRejected` (a genuine rejection) into the
exact same undifferentiated response, discarding the distinction `RpcCommitError.kind` exists to
preserve — exactly the app-team's own concern, and exactly what "typed RpcCommitError.kind is
respected" in the acceptance criteria calls out.

### The fix

1. **gateway.drift**: same "_checked" chain as microflows (`_acquire_lease_checked`,
   `_exec_with_conn_checked`, `_finish_stmt_and_commit_checked`, `_next_checked`,
   `_read_result_doc_checked`, `_call_sp_doc_checked`, all `core.Result<T, RuntimeErrorKind>`, no
   throw) + `_core` versions of `start`/`resume`/`complete`/`fail`, exposed via new interface methods
   `start_checked`/`resume_checked`/`complete_checked`/`fail_checked`. Existing throwing methods
   become one-line wrappers — every other caller (Singular's own e2e/stress/perf tests) is
   unaffected. Compiled clean on the first attempt (`just compile-check` in singular/drift/).
2. **participant-stub/src/app.drift**: all 4 call sites in `handle_put` (`start`, `complete`,
   `resume`, the reclaim-path `complete`) switched to the `_checked` entry points. A new
   `_retriable_response_or_rejected(kind)` helper matches `BackendUnavailable`/`BackendTimeout` ->
   `202 {"state":"pending"}` (the SAME response this file already uses for every other "try again"
   condition — armed-fault pending, a live lease never stolen on reclaim, so the coordinator's own
   retry naturally re-drives the byte-identical PUT); anything else -> `500 {"state":"error","reason":
   "rejected"}`, matching this file's own established `_envelope_error(reason)` convention.
   - **Found along the way**: constructing `throw singular.SingularException(kind = move kind)` (or
     the colon form `singular:SingularException(...)`) from `app.drift` — a DIFFERENT module than
     `gateway.drift` — failed to compile (`E-AUTO-f8fcbb32 "unknown exception event"`), root cause not
     fully traced into driftc's checker. Sidestepped by not reconstructing/re-throwing the foreign
     error type at all — `_retriable_response_or_rejected` is `nothrow` and builds the HTTP response
     directly from the `kind` value. See memory note `drift-cross-module-throw-colon-syntax` for the
     durable record.
   - **`sp_singular_fail` / `fail_checked`**: added to gateway.drift for consistency (same _core
     split), but **not reachable from any current production host/gateway pattern** — grepped the
     whole repo; `.fail()` is called only from Singular's own e2e/stress/perf test files, never from
     `participant-stub` or any other service. This is the "hard external blocker" the acceptance
     criteria allows for: no pinned failpoint test was written for it, because there is no real caller
     to exercise it against meaningfully. Recorded here explicitly, not silently dropped.

### Pinned tests: `microflows/participant-stub/tests/http/failpoint_resilience_test.py` (new)

Reuses `tests/http/conformance.py`'s exact harness pattern (`spawn_stub`, `Stub.put/get`,
`MICROFLOWS_STUB_LEASE_TTL_SECONDS` for a short lease in reclaim scenarios), pointing
`singular.host/port` in the stub's own JSON config at the proxy's data listener — the same "one knob"
mechanism as microflows' manifest-based routing. Ordinals are ground truth via `mysql.general_log`
against the `singular` schema (never guessed):

| Target | Commit ordinal (of N) | Scenario | Result |
|---|---|---|---|
| `sp_singular_start` | 1 of 2 | fresh PUT (start()+complete() in one request) | 202 pending; retry after lease expiry converges, exec_count stays 1 |
| `sp_singular_complete` | 2 of 2 | fresh PUT | 202 pending; retry converges IMMEDIATELY (already Terminal, no lease wait needed), exec_count stays 1 |
| `sp_singular_resume` | 3 of 4 | crash-after-commit reclaim scenario | 202 pending; retry after the RECLAIMED lease's own short expiry converges, exec_count stays 1 |

**A real nuance found for both `start` and `resume`** (not a bug): an ambiguous commit's underlying
write is durable, so it grants a live lease — an IMMEDIATE retry right after correctly sees `Active`
(the lease is genuinely still live) -> 202 pending again, which is NOT itself a failure to converge,
just Singular's own documented "never steal a live lease" design. The pinned tests use a short
`lease_ttl=1` and wait past it before asserting convergence, mirroring the existing
`crash_after_commit_reclaim_on_put` conformance case's own established pattern.

**Full result, 2 consecutive runs:** 20/20 assertions pass across all 3 targets, stable.

### Status: all originally-identified boundaries now covered

Microflows (5 boundaries) + Singular (3 boundaries: start/complete/resume) are fixed and pinned.
`sp_singular_fail` is the one named exception — not reachable in any current host/gateway pattern, so
there is nothing real to fault-inject against; this is a documented, justified exclusion, not a
silent gap.

**Final regression, root-level combined gate** (`just test` at the repo root — singular + microflows
+ microflows/runner + the coordinator-singular integration suite, all in one combined
`drift_test_run.py` plan): **61 ok, 0 failed, 0 skipped, 328.8s.** This is the definitive check —
broader than either component's own standalone `just test` (which only exercises that one component),
it covers Singular's own suite (unaffected by editing its gateway.drift, confirmed) and the
coordinator-singular integration harness (exercising singular + microflows/runner together) in the
same pass. Also independently confirmed both components' own standalone gates: `microflows`'s own
`just test` — 25/25 (drift-test-run) + 156/156 (sp_operation) + 131/131 (sp_call) + 50/50
(call_integration), exit 0.

No regressions anywhere in the monorepo from either the Microflows fix (5 boundaries) or the Singular
fix (3 boundaries) — every existing throwing caller of the touched host.drift/gateway.drift methods is
unaffected, since the "_checked" chain is purely additive (new interface methods + one-line wrappers
around unchanged `_core` logic).

## Final status

**All originally-identified AmbiguousWrite resilience boundaries are covered for this release:**
- Composition: `sp_mf_call_submit`, `sp_mf_checkpoint_reverse_child_reopen`, `sp_mf_checkpoint_reverse_child_settle`
- Participant-dispatch: `sp_mf_operation_request`, `sp_mf_operation_settle`
- Singular: `sp_singular_start`, `sp_singular_complete`, `sp_singular_resume`

8 boundaries fixed, 81 pinned assertions (61 microflows + 20 singular) across two new test files,
stable across repeated runs, zero regressions in the full monorepo gate. The one named exception,
`sp_singular_fail`, is a documented hard blocker (no reachable production caller to fault-inject
against) — not a silently-dropped gap.

## Review findings addressed (2026-07-04, follow-up)

A review of the "done" claim above found 3 real gaps — the fix and pins were real, but "pinned" only
meant "exists as a runnable script," not "part of canonical regression." Addressed:

1. **Neither file was wired into any canonical gate.** Root `just test` (and every component's own
   gate) genuinely passed without ever running either new file — confirmed by grep
   (`tools/emit_test_plan.py`, `microflows/justfile`, `singular/drift/justfile` only reference the
   pre-existing SP regression scripts). Fixed: new root-level `just test-resilience` gate (see
   `justfile`) that owns the *entire* lifecycle — resets both schemas, builds `mfrunner` +
   `participant-stub`, starts `mariadb-failpoint-proxy` with a readiness check (bash `/dev/tcp` probe,
   no python dependency), runs both failpoint test files, tears the proxy down via `trap` (graceful
   SIGTERM — confirmed clean `proxy_shutdown_complete` in the log) regardless of pass/fail. Kept
   deliberately SEPARATE from `test`/`_test-combined` rather than folded in: the proxy binary comes
   from a sibling `drift-mariadb-client` checkout that is not a declared cert capability
   (`requires:["tool:mariachi","tool:docker"]` only), and this gate is materially slower — folding it
   into the cert-critical path would violate the "no behavior change" constraint on `test`/
   `test-singular`/`test-microflows`/`test-integration` those recipes already carry. Verified
   `test`/`test-singular`/`test-microflows` are byte-identical (`just --show`) before/after this
   addition. Two consecutive full runs of `just test-resilience`: 61 + 20 = 81 passed, 0 failed,
   `EXIT_CODE=0` both times.
2. **The Singular failpoint test file was untracked in git.** Confirmed via `git status --short` (`??`).
   Left to the user — git staging in this repo is explicit-permission-only, and the user declined when
   asked, choosing to handle it themselves.
3. **Both files required an externally-running proxy for manual use.** Closed by (1): the new gate
   owns the proxy's full lifecycle itself now, so this is no longer a precondition for calling the
   suite "pinned regression" — it's just how the gate works internally. The files' own docstrings still
   describe manual invocation for fast local iteration (unchanged, still useful), but the canonical
   path is `just test-resilience`.

Also added `work/workflows-failpoint-resilience/README.md` (the charter) — this effort previously only
had `DESIGN.md`+`PROGRESS.md`, missing the repo's own `work/README.md` convention.

## Round-2 review: gate correctness + doc staleness (2026-07-04, same day)

3 more findings, all addressed:

1. **HIGH — `test-resilience` used `exec` for its flocker call, silently skipping DB-teardown.**
   `justfile`'s `test-resilience` installed the entry-state-restoring EXIT trap, then did
   `exec "{{FLOCKER}}" ... -- just _test-resilience-locked`. `exec` replaces the shell's own process
   image — the EXIT trap registered earlier in that same script never fires, because the bash process
   that would run it no longer exists. If this recipe created or started the DB, it could leave it
   running instead of restoring entry state. **Fixed**: removed `exec`, runs flocker as a plain
   foreground command (`set -e` still propagates its exit code identically, just via normal script exit
   instead of process replacement, which lets the trap fire on the way out). **Verified concretely, not
   just re-read**: `just db-down` (DB fully absent) → `just test-resilience` → `just db-status` reported
   `absent` again afterward (previously, with `exec`, it would have stayed running). 81/81 passed,
   `EXIT_CODE=0`, same as before the fix.
2. **HIGH — two files still untracked**: the Singular test file (round 1) plus the new `README.md`.
   `AGENTS.md`'s "Git usage (strict)" already covers this plainly (no staging without explicit
   permission) — left to the user, not raised again.
3. **LOW — `README.md` had stale "not yet gated" / open-question wording** left over from before the
   gate landed (written mid-effort, never updated after `test-resilience` shipped). Fixed: plan item 5,
   "files affected," and "open questions" sections now reflect the resolved state.
