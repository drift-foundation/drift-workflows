# Microflows Handoff Roadmap (MVP → product)

**Status:** pinned high-level roadmap (2026-06-19). Details are discussed per slice; this file fixes
the **order and intent** so we keep moving toward a *usable product for business app teams*, not just
an interesting runtime.

**Product goal.** Hand Microflows to the business app team (e.g. PushCoin Bookkeeper) for **dev,
testing, and eventual production use**. That means prioritizing **authoring ergonomics, deployment/
update safety, packaging, and examples** over unrelated runtime exploration. Every slice below is
chosen against that goal.

See also: the as-built runtime + language (`microflows_design.md` §12), the authoring guide
(`microflows_user_guide.md`), and the V1 capability envelope / limits (§12.7).

---

## The order

### 1. Expression object/array construction — ✅ LANDED (2026-06-19)
Removed the immediate authoring blocker for real workflows: **assembling participant inputs from
args/results/locals without pre-shaping everything**. Object/array literals with expression-valued
fields/elements (`{ customer: arg c.id, amount: arg order.amount }`), pure, type-checked, replay-safe;
fully-constant literals fold to a const (hash-stable). Full gate green — integration **138/138**, unit
base + asan. As-built: `microflows_design.md` §12.9.

### 2. Operational admission / draining / reload behavior — ✅ LANDED (first pass, 2026-06-19)
Required for any reasonable **prod update cycle**. **First pass (policy + shape, not full machinery):**
admission state (`accepting` / `draining` / `stopped`) is an **input to the drive boundary** (`_run`,
via `MICROFLOWS_ADMISSION`). While draining/stopped: a **fresh submission is refused** before
create/claim/dispatch (`{"workflow":"refused","reason":"draining"}`, nonzero exit); **existing work
resumes** but a would-be defer returns `{"workflow":"pending_restart",…}` (no new retry scheduled) so
the drain converges. Same gate serves reload + graceful shutdown. The future front-door maps
refused/pending_restart → **HTTP 503** (no `Retry-After` yet). See `microflows_design.md` §13.

### 2.5. `drive_workflow(...) -> Outcome` library extraction — ✅ LANDED (2026-06-19)
The drive now **returns a structured `Outcome`** instead of printing JSON inline. A typed `Outcome`
variant (`Completed` / `Reversed` / `Deferred` / `Aborted` / `Refused` / `PendingRestart` / … — 17 arms)
captures every machine-readable status; `_oc_render` is the single source of the JSON and `_oc_exit` the
single source of the exit code. All ~61 inline `console.println("{…}")` + bare-`Int` sites across the 13
drive functions (`_run`, `_run_planned`, `_run_forward`, `_run_reversal`, `_compensate` via `CompStep`,
the `_defer*`/`_fail_operation`/`_reverse_block`/`_inspect_report`/`_report_terminal` helpers) were
converted to `return Outcome::…`; the CLI adapter (`main`) renders **once** via `_emit`. A future service
renders the same `Outcome` to HTTP. **Byte-compatible** — JSON shape and exit codes unchanged (the
integration suite, **142/142**, was the oracle, run on each of the two verifiable passes). The
`_run`/drive boundary is now the coordinator-library entry the front-door (item 3 / the service) calls
instead of shelling around the CLI.

### 3a. ScriptRegistry packaging / manifest (one-shot CLI) — ✅ LANDED (2026-06-19)
App teams deploy **named, pinned, validated workflow revisions** instead of ad-hoc lowered configs. A
deployment **manifest** (`{ "deployment": {db, participants, operations}, "scripts": [{name, version,
path}] }`) declares named `.mf` scripts over a shared deployment. `mfrunner --manifest <file>`
**compiles+validates EVERY declared script at startup** (lower over the deployment → build → validate)
and **fails fast** on any missing/unreadable/invalid script or duplicate name (`invalid_manifest`). A
SUBMISSION names a script (`--script <name>`); creation **pins its resolved identity** (script name,
plan version, content_hash, plan_length). A RESUME drives **strictly by the durable pin** (the script
matching the pin's name+version) — never the manifest's active version (no silent substitution). One
deployment block (per-script routing is a later refinement). The drive itself is unchanged — the
manifest resolves the one script and calls the same `_run_cfg` boundary. Full gate green —
integration **149/149** (C20: named submit + pin/hash parity, unknown-script refused, fail-fast
invalid-manifest, resume-by-pin). As-built: `microflows_design.md` §14.

### 3b. ScriptRegistry service shell — ✅ LANDED (2026-06-20)
The thin long-running front-door: a second artifact (`uflowsd`) on `web.rest` that owns the
in-memory **swappable** registry and ONE shared, internally-pooled host, serving submit/resume/health
over HTTP. Each request calls the SAME `_drive_manifest_request` → `_run_core` (drive_workflow) →
Outcome the CLI uses — no new workflow semantics; `_run_core` was extracted so the host is built ONCE
at startup and shared (per-workflow leases/fencing keep concurrent drives safe). Owns runtime-mutable
admission: **SIGTERM → draining** (then graceful `rest.shutdown`), enforced by the same drive rules
(fresh submission while draining → Refused → **HTTP 503**; `/readyz` → 503). **Staged reload on
SIGUSR1**: load+validate a new manifest into a standby, atomically swap on success, keep the old on
failure. Outcome → semantic HTTP status, body the EXACT Outcome JSON (contract unchanged), so CLI and
HTTP consumers read identical documents. **Internal API only** — the `/v1/workflows` route group is the
seam where item-5 auth middleware / a security context attaches (no auth logic built). Full gate green
— integration **158/158** (C21: health/ready, submit→completed, resume→terminal replay, unknown-script
400, SIGUSR1 reload swaps the registry, draining→503). As-built: `microflows_design.md` §15.

### 4. Business-team starter kit — ✅ LANDED (2026-06-22)
Runnable, production-shaped templates an app team copies for its first workflow (`microflows/examples/`):
five `.mf` workflows — `payment_authorize_capture`, `payment_refund`, `inventory_reserve_release`,
`account_adjustment_with_rollback`, and a mixed `checkout_branch_merge` (branch + merge + compensation) —
over ONE shared deployment/routing registry + a service `manifest.json` (three logical participants
payments/inventory/accounts, operation + compensation contracts, `auth_profile: null`), plus a
`RUN_LOCAL.md` runbook (submit/resume/reload over HTTP) with an explicit **security-boundary** section
listing the open questions for the business team. The reference participant stub gained the
payment/inventory/account operations (realistic payloads, deterministic ids) + an out-of-band fault
control, so the example payloads stay clean. Proven by integration C21 (`ex_*`): success,
later-step→compensation, refund, pending→resume, branch+merge checkout, reload-preserves-pin, terminal
replay with the participant down. Full gate green — integration **165/165** on driftc 0.33.53 / abi 18.
As-built: `microflows/examples/README.md` + `RUN_LOCAL.md`.

> **Surfaced + fixed a production bug.** The 4-op `checkout` exposed a `web.rest` keep-alive
> epoll-readiness defect (~2.3s/dispatch, alternating connection failures; the long-running service
> degraded). Reported to the web team with a minimal DB-free repro; fixed in **web-rest 0.5.6 /
> driftc 0.33.53**. No Microflows change was needed — re-validating on the new toolchain unblocked it.

### 4.5. Workflow composition — ✅ LANDED (2026-07-02)
A parent workflow step can `call child@<plan_version> { … }` and await the child's terminal outcome
as an ordinary durable step (typed args in, typed return out) — a **single async workflow call**,
not fan-out. A blocked/non-terminal child never cascades a block up the call tree; the parent simply
stays `pending` on it (§0/§4 of `work/workflow-composition/DESIGN.md`). If the parent itself later
reverses, its call checkpoint drives **reverse-child compensation**: the (already-completed) child is
durably reopened into its own reversal and asked to compensate itself — recursively, through
arbitrarily nested call chains, via the same generic reversal machinery every level already has, with
no parent enumeration of a child's internal checkpoints (`work/workflow-composition/1c-design.md`).
`microflows-viz` (`microflows-viz/`, successor to the retired `mfinspect` CLI), a read-only operator tool for a workflow's full call/event
tree, was pulled forward ahead of this work since the reversal-across-a-tree integration debugging
needed it immediately. Full gate green — `microflows` unit/e2e/SP/integration suites (SP regression
131/131, runner-level `call_integration_test.py` 50/50, including a nested A→B→C acceptance case with
a DB-level assertion that no level's audit trail references a grandchild's identifiers). Explicitly
**out of MVP scope** (deferred, not forgotten): fan-out, `on failed`/failure-as-data, a stuck-child
liveness budget, and a separate compensating-workflow mode. As-built:
`work/workflow-composition/DESIGN.md`, `work/workflow-composition/1c-design.md`, and
`work/workflow-composition/PROGRESS.md` (the authoritative, current status).

### 5. Participant auth / security context reference
Important for prod, but **defer the design until the app team gives us real requirements** — we should
not guess the security model now. (Today `auth_profile` must be null/absent; see `security_model.md`
and the §12.7 limit.) This is the known production blocker for open-wire money movement, sequenced last
deliberately so it is designed against real needs, not speculation.

---

## Why this order

Authoring ergonomics first (1) so workflows are writable at all; then the deployment/update safety (2)
and packaging (3) that a team needs to *operate* it; then examples (4) that make it learnable; then
security (5), designed against real requirements rather than guessed. Runtime features unrelated to
this path (e.g. `while` loops, in-loop remote calls, variable per-branch op counts) stay deferred
unless an app-team requirement pulls them in.
