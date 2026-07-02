# 1b.1 runner-runtime wiring — integration test design (for review)

## Context

The 1b.1 runner-runtime wiring (`NCallWorkflow` dispatch, await-the-child, recovery/replay,
`_run_reversal`'s `call_kind` branch) is implemented and the runner builds clean. The last item in
scope is integration coverage of 5 scenarios: child completes, child fails, child blocked/non-terminal,
replay creates no second child, call checkpoint reverses as a no-op.

This doc covers ONLY the test-approach decision that came up while designing that coverage — not the
runtime implementation itself.

## The problem

The new dispatch code (`_run_forward`'s `NeedCall` handling, `_run_reversal`'s `call_kind` branch) lives
in non-`pub` functions inside `runner.drift`. The only way to exercise it is through the actual CLI entry
point (`main()` → `_run_manifest`/`_run_core` → `_run_planned` → `_run_forward`) — a Drift test file in a
different module (like the existing host-level `live_call_test.drift`) cannot call these functions
directly; it can only reach the host API, which doesn't touch the new runner code at all.

A single CLI invocation drives ONE workflow instance to its next durable boundary and exits. The parent
and the child it calls are two independently-tracked workflow instances. Testing "child completes, parent
consumes result" genuinely requires three separate process invocations of the compiled `mfrunner` binary
(renamed from `microflows-runner` in this same pass — scoped to the runner package: build output,
`cli.parser`/logger self-description, and the two existing test harnesses; the Drift package/artifact
identity in `drift/lock.json` and references outside `microflows/runner/` were left untouched):

1. Submit the parent (`--workflow-id P --script parent --arguments {}`) → creates the child, parent
   comes back "pending" (child isn't terminal yet).
2. Drive the child forward directly (`--workflow-id <child-id>`, a resume — no `--script`/`--arguments`).
3. Resume the parent (`--workflow-id P`) → it observes the now-terminal child and settles/reverses.

Step 1 creates the child's id internally (a UUIDv3 derivation, domain-tagged `mf-call;`, computed inside
the Drift binary). Step 3 needs that id. The question was: how does the test (the thing gluing these
three subprocess calls together) learn the child's id in between?

## Options considered

**A. Query the DB directly between steps.** After step 1, `SELECT HEX(child_workflow_id) FROM
tb_mf_workflow_operation_call WHERE workflow_id = ... AND operation_seq = 1`. Matches the existing test
convention (`sp_call_test.py` already queries tables directly for verification), but reaches into an
internal table specifically for flow control, not just assertion.

**B. Have the CLI's own JSON output carry the child id forward (the "carrier" pattern).** Step 1's stdout
already has the shape `{"workflow":"pending","reason":"...","defer_until":"..."}`. The runner's own
`NeedCall` handling calls `host.call_inspect(...)` before deciding to defer, and that call already returns
`child_workflow_id` — the value is sitting in memory, just not being surfaced. Add it to the rendered JSON
(only when the pending is call-related), and the test reads it straight from step 1's own stdout instead of
touching the DB. Step 2 becomes purely mechanical: parse stdout, pass the id to the next invocation.

**C. Sidekick file — write a second output file alongside stdout carrying richer metadata, leaving stdout
untouched.** Raised as an alternative to B, motivated by an initial (incorrect) assumption that the JSON
contract can't be extended at all. Rejected — see below.

## Why B over A

The production model is poll-based, not handoff-based: a worker pool scans `tb_mf_workflow` for rows
where `next_attempt_at <= now` and claims/drives whichever it finds — parent and child alike. Nothing in
production ever "hands" a child id to a caller because nothing in production needs it handed — durable
state is the only coupling between a parent and its child. Option A's DB query stands in for what a
real poller would discover on its own; it's a reasonable test expedient, but it's specific to
`tb_mf_workflow_operation_call`'s schema and reaches past the sanctioned interfaces (CLI stdout, host API)
that any other external caller would be limited to.

Option B doesn't just serve the test — it fixes a real observability gap. Any external orchestrator or
monitoring tool driving this system would have the identical need ("which child is parent P's call op
currently waiting on?") and currently has no way to answer it without reaching into internal tables
either. B costs nothing extra at runtime (`call_inspect` is already called on that path); it just stops
discarding a value that's already been fetched.

## Why B over C (sidekick file)

`_oc_render`'s doc comment states the JSON shape is "byte-compatible with the prior inline form (pinned
by the integration suite)," which initially suggested touching it was unsafe — hence considering a second
channel (C).

On closer inspection, `_oc_render` already has precedent for conditionally-rendered fields on this exact
function: `Deferred` only includes `defer_until` when non-empty; `Blocked` only includes `reason` when
non-empty. Adding `child_workflow_id` to `Pending`, rendered only when present, follows the same
established discipline — and since composition dispatch didn't exist before this feature, nothing could
have been pinned against a call-related `Pending` shape. The byte-compatibility concern only applies to
paths observable before this change; this is a new path.

Given that, a sidekick file would be net-new surface for no real benefit: a new CLI flag, a file-naming/
discovery convention, atomicity and cleanup questions — none of which exist anywhere in this codebase
today — to solve a problem that a one-field, conditionally-rendered JSON addition already solves within
the file's own existing style. Introducing a second observable channel also means every consumer now has
two things to correlate instead of one.

## Decision

Extend `Outcome::Pending` to carry `child_workflow_id: String` (empty when not applicable), rendered only
when non-empty — same pattern as `Deferred`/`Blocked`. The value comes from the `CallInspectOutcome::Found`
the `NeedCall` handling already receives; no new DB read, no new call.

The integration test then becomes a genuine "carrier" flow: each subprocess invocation's stdout JSON
supplies whatever the next invocation needs (a workflow-id to target next), with the DB used only for
out-of-band *assertions* (e.g., proving replay didn't create a second child row), never for control flow.

## Team review (approved)

No blocking findings. Confirmed option B is correct, with one guardrail: the call-wait path reaches
`Pending` through `_defer_pending`, which releases the parent's lease and sets `next_attempt_at` — the
implementation must not bypass that release path just to attach the child id. `_defer_pending` was
parameterized with `child_workflow_id_hex: &String` (empty for every pre-existing non-call caller) rather
than reconstructing the outcome separately, so the release/lease/admission-gate semantics are provably
unchanged for every existing call site.

DB reads remain fine for test *assertions* (e.g., proving replay creates no second child row), just not
for driving the subprocess sequence.

## Status

Implemented: `Outcome::Pending` carries `child_workflow_id` (rendered only when non-empty, matching
`Deferred`/`Blocked`'s existing style); `_defer_pending` parameterized accordingly; all 7 call sites
updated (6 pass `""`, the `NeedCall` non-terminal-child arm passes the value already returned by
`call_inspect`). Runner builds clean. Binary renamed to `mfrunner` (scope: runner package only, per
separate decision — `MF_RUNNER_BIN` env var name kept as-is). Integration test scenarios not yet written.
