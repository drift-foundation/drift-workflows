# Workflow composition resilience validation via mariadb-failpoint-proxy

## Charter

Not a new feature. The certified workflow-composition MVP (`singular` 0.8.0, `microflows` 0.6.0,
`uflowsd` 0.5.0) has never been validated against the one class of fault its own `RpcCommitError.kind`
handling exists for: a COMMIT whose acknowledgement is lost (`AmbiguousWrite`) or never sent (`NotSent`).
Every existing test proves the code branches correctly *in theory* (matches the documented `kind`
contract) and doesn't regress under normal conditions — none of them ever actually induce an ambiguous
commit and watch the system recover. `drift-mariadb-client`'s new `mariadb-failpoint-proxy` (0.2.1) is
exactly the tool for closing that gap: a transparent MariaDB-wire proxy with a one-shot,
targetable ack-loss failpoint, controllable over a raw TCP JSON-Lines protocol. Full proxy reference:
`drift-mariadb-client/docs/failpoint-proxy-usage.md`.

## Plan (user-directed)

1. **Baseline, no proxy.** Confirm current behavior: a small composed workflow (parent calls child)
   completes cleanly, and a separate one reverses cleanly.
2. **Wire MariaDB traffic through the proxy.** Route the domain under test through
   `mariadb-failpoint-proxy`'s data listener; everything else stays direct.
3. **Inject `AmbiguousWrite` on targeted COMMITs**, one at a time:
   - Singular claim/start/complete paths.
   - Microflows operation settle.
   - Workflow call submit / child create.
   - Reverse-child reopen / settle.
4. **Verify outcomes** for each injected case:
   - no duplicate child workflow creation
   - no duplicate participant execution beyond idempotent replay
   - no stuck parent except expected pending/retry
   - terminal replay remains stable
   - `mfinspect` can explain the tree after recovery
5. **Add pinned tests** for any gap found.

**Starting case (smallest, highest-value):** lose the COMMIT ack after `sp_mf_call_submit` — the atomic
parent-op + child-workflow-creation boundary. If retry is correct there, we've proven a key composition
invariant under the hardest DB ambiguity window (the point where two durable rows — the parent's call
operation and the child's own workflow row — must appear atomically or not at all, and a client that
never learns whether that commit landed must be safe to retry).

## Key mechanics (from `mariadb-failpoint-proxy` + its usage doc)

- **Topology:** `app -- MariaDB wire --> proxy -- MariaDB wire --> real MariaDB`, plus a separate raw-TCP
  JSON-Lines control listener the test harness talks to. Three ports: `--data-host/port` (what the app
  under test connects to), `--backend-host/port` (the real DB), `--control-host/port` (test harness).
- **Route only the domain under test.** Don't put multiple domains behind one proxy instance unless
  there's a stronger discriminator than "next COMMIT." We have two domains that matter here:
  `microflows` (coordinator control state — operation settle, call submit, reverse-child reopen/settle)
  and `singular` (participant idempotency store — claim/start/complete). Each targeted case in step 3
  proxies exactly one of these; the other stays direct.
- **Control protocol** (`health`/`clear`/`arm`/`assert_all_fired`/`status`/`list`, one JSON object per
  line, synchronous request/response): `arm` takes `match.nth` (which COMMIT ordinal to hit, default 1,
  counted from arm-time not connection-open) and `action` — `drop_server_response_after_forward`
  (default, reset-style: proxy closes immediately after forwarding) or `drop_and_hold_after_forward`
  (timeout-style, needs `hold_ms`: proxy holds the connection open, discarding responses, before
  self-closing — proves the slow/hung-network flavor classifies identically to a reset).
  `assert_all_fired` fails loudly if nothing was ever armed, not just if an armed one never fired.
- **`SIGKILL` only.** The proxy has no graceful shutdown or signal handler; needs `driftc >= 0.33.68`
  toolchain to even avoid busy-spinning on SIGTERM/SIGINT (already the case for our certified toolchain —
  see the drift-lang cert announcement this session).
- **Pool config for determinism:** `max_conns = 1` (unambiguous "next COMMIT"), no keepalive traffic, a
  finite `acquire_timeout`.

## How this plugs into drift-workflows specifically

Unlike `drift-mariadb-client`'s own proxy tests (which call `rpc.commit()` directly and assert on
`RpcCommitErrorKind` in Drift), our fault has to be exercised **through the application**: `host.drift`'s
`_finish_stmt_and_commit` already classifies `RpcCommitError.kind` internally (fixed this session — see
`RELEASE_ANNOUNCEMENT_DRAFT.md`'s "Post-draft fixes"). What we're validating here is not that
classification (already covered) but the **behavior above it** — does the operation/workflow actually
end up in a safe, recoverable state once the ambiguous commit is classified as retriable.

The practical hook: `mfrunner`'s DB connection isn't configured via env vars, it's read from the
manifest JSON itself (`deployment.db.host`/`deployment.db.port`) — see
`microflows/db-tests/call_integration_test.py`'s `write_manifest()`. Pointing that at the proxy's data
listener (with `--backend-host/port` forwarding to the real dev DB) routes 100% of a driven
`mfrunner` invocation's `microflows`-domain traffic through the proxy, with zero changes to `mfrunner`
itself. This means the harness can be built entirely in Python, reusing `call_integration_test.py`'s
existing patterns (`write_manifest`, `run_cli`, the in-process stub participant server) plus a small
new proxy-control-client helper (~20-30 lines of stdlib `socket`+`json` — much simpler than the ~80-line
Drift version `drift-mariadb-client`'s own tests duplicate per-file, since Python has native JSON/socket
support) — no new `.drift` test files needed for the microflows-side cases.

For the `singular` claim/start/complete cases, the equivalent hook is whatever builds `singular`'s own
RPC connection config in `participant-stub`/the coordinator-singular harness — TBD when we reach that
step; likely the same shape (a config the harness controls, not baked into `singular`'s own source).

## Verification model

Each case in step 3/4 doesn't assert on `RpcCommitErrorKind` directly (that's `host.drift`'s job, already
covered) — it asserts on **observable system state** after the fault: query the relevant `tb_mf_*` /
`tb_singular_*` rows directly (or drive `mfinspect`) to confirm exactly one child workflow exists, exactly
one participant execution happened (or an idempotent replay of the same one), the parent isn't stuck in
an unexpected state, and a repeat `mfinspect` dump renders a coherent, explainable tree.

## The fix pattern (after the first finding)

The first `sp_mf_call_submit` case found a real defect: `HostException(BackendUnavailable)` from a
commit-ambiguous write propagated uncaught to `main()`'s top-level catch (`runner: fatal`), and the
workflow's lease sat locked for its full TTL rather than being released/deferred promptly. Root cause
went one level deeper than expected: the *existing* `_core`/`*_best_effort` split
(`_call_hint_refresh_core`, `_child_terminal_notify_core`) only catches `managed:ManagedError` — it does
NOT catch the `HostException` thrown directly by `_finish_stmt_and_commit`/`_exec_with_conn`/`_next`,
because `HostException.kind` (a nested `HostErrorKind` variant) can't be projected through a typed
`catch HostException(kind)` in this toolchain (`E_TYPED_CATCH_FIELD_UNSUPPORTED_TYPE` — scalar fields
only). So even the established pattern doesn't close this gap on its own.

**The actual fix, applied once and reused for all 5 targeted call sites:**
- New "_checked" siblings of every low-level throwing helper (`_acquire_conn_checked`,
  `_exec_with_conn_checked`, `_next_checked`, `_finish_stmt_and_commit_checked`,
  `_read_result_doc_checked`, `_call_sp_doc_checked`) — each returns `core.Result<T, HostErrorKind>`
  instead of throwing. No typed-catch anywhere in this chain, so no projection limit applies (a
  `HostErrorKind` returned via a plain `core.Result::Err` match is just a value, freely matchable).
  Deliberately narrower than full fidelity: `_str_req`/`_parse_object_doc`'s malformed-data paths still
  throw (a data-integrity bug, not a transient network condition — not something the dispatch loop
  should try to gracefully retry).
- A `_core` version of each of the 5 target host methods (`call_submit`, `operation_request`,
  `operation_settle`, `checkpoint_reverse_child_reopen`, `checkpoint_reverse_child_settle`), built on
  the checked chain, returning `core.Result<Outcome, HostErrorKind>`. The existing throwing public
  method becomes a one-line wrapper (`match _core(...) { Ok(v) => v, Err(k) => throw
  HostException(kind=move k) }`), so every other existing caller is unaffected.
  - Shared runner.drift helper `_defer_on_backend_unavailable(host, workflow_id, fencing_token, kind,
    reason_prefix, admission)`: matches `BackendUnavailable`/`BackendTimeout` → `_defer(...)` (generic
    `host.release`-based short defer, direction-agnostic — the SAME mechanism `EventTimeSkew`'s
    handler already uses in both forward and reverse contexts); anything else → re-throw unchanged
    (`default => throw HostException(kind = move kind)`), preserving existing non-retriable behavior
    exactly. (An earlier draft used `_defer_dispatch` instead — this crashed for
    `checkpoint_reverse_child_reopen`'s pinned test; see PROGRESS.md for the full story.)
  - Each of the 5 runner.drift call sites: `match <core>(...) { Ok(outcome) => <existing match>,
    Err(kind) => return _defer_on_backend_unavailable(host, workflow_id, fencing_token, move kind,
    "<site-name>", admission) }`.

## Singular increment (2026-07-04)

Step 3's first bullet ("Singular claim/start/complete paths") was originally left uncovered when the
Microflows pass shipped — closed in a follow-up per explicit user direction (a documented-but-open gap
was not acceptable for this release). `singular/drift/packages/singular/src/gateway.drift` mirrors
`host.drift`'s architecture exactly, so the same "_checked" chain fix applies directly; the production
caller is `microflows/participant-stub` (the PushCoin-participant reference), whose `web.rest`
framework already has a generic per-request 500 catch-all (no process-crash surface like mfrunner's,
but it *did* collapse `AmbiguousWrite`/`ServerRejected` into the same undifferentiated response —
the actual gap). Full story, fix, and pinned-test results in `PROGRESS.md`'s "Singular
claim/start/complete increment" section.

## Status: DONE

All originally-identified boundaries covered: Microflows (5: call_submit, operation_request,
operation_settle, checkpoint_reverse_child_reopen/_settle) + Singular (3: start, complete, resume).
`sp_singular_fail` is the one named, justified exclusion (unreachable in any current host pattern).
81 pinned assertions (61 + 20) across two new test files, all stable. Root-level combined `just test`
(singular + microflows + runner + coordinator-singular integration): 61 ok, 0 failed, 328.8s — no
regressions anywhere.

Wired into a canonical gate per review: new root-level `just test-resilience` owns the
`mariadb-failpoint-proxy` lifecycle end to end (build discovery, start, readiness probe, teardown via
`trap`) and runs both pinned test files — 81/81 across 2 consecutive runs, `EXIT_CODE=0`. Deliberately
separate from `test`/`_test-combined` (proxy comes from a sibling repo, not a cert capability;
materially slower); `test`/`test-singular`/`test-microflows` confirmed unchanged. See `README.md` for
the charter and `PROGRESS.md` for the full day-to-day story and results.
