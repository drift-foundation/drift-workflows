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
path}] }`) declares named `.mf` scripts over a shared deployment. `microflows-runner --manifest <file>`
**compiles+validates EVERY declared script at startup** (lower over the deployment → build → validate)
and **fails fast** on any missing/unreadable/invalid script or duplicate name (`invalid_manifest`). A
SUBMISSION names a script (`--script <name>`); creation **pins its resolved identity** (script name,
plan version, content_hash, plan_length). A RESUME drives **strictly by the durable pin** (the script
matching the pin's name+version) — never the manifest's active version (no silent substitution). One
deployment block (per-script routing is a later refinement). The drive itself is unchanged — the
manifest resolves the one script and calls the same `_run_cfg` boundary. Full gate green —
integration **149/149** (C20: named submit + pin/hash parity, unknown-script refused, fail-fast
invalid-manifest, resume-by-pin). As-built: `microflows_design.md` §14.

### 3b. ScriptRegistry service shell — *next (the long-running front-door)*
The thin long-running wrapper: a service that owns the registry, **atomic/staged reload** (SIGHUP/
SIGUSR1, §4.1), the **admission gate** (item 2), and a submit/resume entry calling **`drive_workflow`
→ Outcome** (item 2.5) — rendering admission/drain → HTTP 503 and outcomes → HTTP. Now a thin wrapper
around three landed pieces (registry + admission + the Outcome boundary), not a redesign.

### 4. Business-team starter kit
Examples and fixtures for **realistic workflows** so a new team is productive fast: charge / update /
refund, inventory reservation, account mutation, **failure injection**, a local **runbook**, and
**diagnostics expectations**. Builds on the authoring guide; turns it into runnable templates.

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
