> **Imported from `pushcoin-v3/components/microflows/docs/` — the canonical Microflows (Java) design of record.**
> Microflows is still implemented in Java; a Drift port is a planned future job, and this
> `microflows/` tree is its new home in this repo. This file is preserved as-is for design intent.
> For how the protocol is **actually implemented today** by the Drift `bookkeeper` (and where it
> diverges), plus the `microflow-proto-check` conformance task and the deploy gate, see
> [README.md](README.md).

# Microflows Protocol Cheat Sheet

## Core Flow
	- Clients `POST /work-orders` with stable `wo_id` and optional `policy`/`meta` blocks; MF persists `async` work, in-memory handles `sync` ones.
	- Inline success: downstream returns 2xx inside the sync budget; MF emits `SUBMIT` + `FINISHED` and replies with the downstream body.
	- Async fallback: downstream returns 202, posts `IN_PROGRESS`, or the inline timer expires; MF responds 202 `ACCEPTED`, stores the work order, and resumes via heartbeats.
	- Clients poll `GET /work-orders/{wo_id}` for combined status/events; downstream services optionally receive feedback callbacks.

## Task Expectations
	- Every task must expose `endpoint`, `status_endpoint`, and optional `feedback_endpoint`; payloads include a `meta` block that downstream must echo.
	- Downstream calls must be idempotent. MF may retry on transport errors, recoverable failures, or after crash recovery.
	- Retry policy comes from task fields (`retry.strategy`, `initial_delay_ms`, `max_attempts`, etc.); `heartbeat_timeout_ms` controls how long MF waits before probing `status_endpoint`.
	- Dependencies (`depends_on`) gate execution; default trigger is upstream `FINISHED`, with optional `on: failure|done` overrides.

## Status & Event Mapping
	- `SUBMIT`: MF attempted the task; HTTP 2xx marks terminal success unless body flags `recoverable`.
	- `ACCEPTED`: downstream acknowledged receipt or inline budget expired; MF will monitor heartbeats/retries.
	- `CALLING`: request in flight; watchdog converts to `REQUEUED` if the call stalls.
	- `RETRYING`/`REQUEUED`: retry loop engaged using configured backoff.
	- `IN_PROGRESS`: downstream heartbeat keeping the task alive.
	- Terminal: `FINISHED`, `FAILED`, `TIMED-OUT`, `CANCELLED`, `LATE_SUCCESS`, `LATE_FAILURE`.

## Downstream Contract Quick Ref
	- Success → `200 OK` with final payload; echo `meta`.
	- Still running → `202 Accepted`; include optional heartbeat payload.
	- Never saw task → `404`; MF resubmits.
	- Hard failure → relevant 4xx with non-recoverable body.
	- Recoverable failure → include `"recoverable": true` at top level or under `error`.
	- Feedback endpoint receives `{ meta, status, response }` once MF commits the outcome.

## Review Checklist
	1. Confirm `summary.json` shows expected sequence per scenario (e.g., dependencies, retries, callbacks).
	2. Compare `normalized/` artefacts against policy expectations (timing masked, but order/status should match design).
	3. Inspect log snippet (`logs/microflows.log`) for work-order-specific entries: submissions, dispatch, retries, feedback posts.
	4. Verify reconstructed downstream payloads honour idempotency/echo `meta`.
	5. Mark `PASS` only when transitions align with the intended mode (`sync` vs `async`) **and** no anomalies appear in logs or payloads.
