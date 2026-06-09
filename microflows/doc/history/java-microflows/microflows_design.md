> **Imported from `pushcoin-v3/components/microflows/docs/` — the canonical Microflows (Java) design of record.**
> Microflows is still implemented in Java; a Drift port is a planned future job, and this
> `microflows/` tree is its new home in this repo. This file is preserved as-is for design intent.
> For how the protocol is **actually implemented today** by the Drift `bookkeeper` (and where it
> diverges), plus the `microflow-proto-check` conformance task and the deploy gate, see
> [README.md](README.md).

## Microflows Service Design

### Purpose
Microflows (MF) is the submission orchestration layer between clients and downstream services such as Bookkeeper. It enforces consistency in intake, persistence, retry, and status reporting so domain services remain thin request handlers.

### Layer Responsibilities
	- Web client: submit requests until it receives `ACCEPTED` or `FINISHED`; store the supplied `submission_id` to poll MF later.
	- API gateway: authenticate, authorize, validate payloads, enrich requests with context, and forward to MF.
	- Microflows: coordinate delivery to services, manage retries/timeouts, persist queued submissions when operating asynchronously, and expose status APIs.
	- Bookkeeper: accept work via HTTP, execute business logic, and surface current status through its own HTTP endpoint.

### Submission Envelope
Incoming payloads contain a required identifier plus optional metadata blocks:

```json
{
	"wo_id": "<client-supplied-uuid>",
	"wo_name": "customer_payment",
	"meta": {
		"auth": {
			"account_id": "school-tenant-id",
			"user_id": "operator-id"
		}
	}
}
```

	- `meta.auth`: attached by the API gateway so tenants can scope queries (`account_id`) and keep an audit trail for operators (`user_id`). The dispatcher persists this block verbatim, enabling future reporting without MF imposing a schema beyond the JSON contract. Omit the block when scoping is unnecessary.
	- `wo_id`: client-supplied UUID that remains stable across retries; MF stores it as `BINARY(16)`.

### Submission Policy Schema (v1)
The optional `policy` block lets clients choose between synchronous completion and queued processing:

```json
{
	"policy": {
		"mode": "async",
		"sync_response_timeout_ms": 500,
		"task_defaults": {
			"call_timeout_ms": 2000,
			"retry": {
				"strategy": "exponential_backoff",
				"initial_delay_ms": 500,
				"multiplier": 2.0,
				"max_delay_ms": 60000,
				"max_attempts": 8,
				"jitter_ms": 250
			},
			"heartbeat_timeout_ms": 3000,
			"completion_deadline_ms": 86400000
		}
	}
}
```

	- `mode`:
		- `async` (default) tries to finish inline but persists the work order if a task runs longer than the inline budget or returns an in-progress response. Once persisted, the heartbeat and retry loop take over.
		- `sync` keeps the work order in-memory; MF waits for each task to return a terminal result and responds to the client immediately. If the call times out or MF restarts mid-flight, the client must retry—no queue entry is written.
	- `sync_response_timeout_ms`: best-effort budget for inline handling. When the budget expires under `async` mode, MF returns `202 ACCEPTED` and continues asynchronously. The setting is ignored in strict `sync` mode because the request either finishes within the HTTP round trip or it fails fast.
	- `task_defaults`: baseline per-task values (`call_timeout_ms`, retry `strategy`, `heartbeat_timeout_ms`, `completion_deadline_ms`) applied when a task omits an override.

### Synchronous vs asynchronous behaviour

- Inline success (any mode): if a task returns a terminal 2xx before the timeout, MF records `SUBMIT`/`FINISHED` and immediately posts feedback. The work order never leaves memory.
- Async continuation (`async` mode): if the downstream responds with `202`, posts `IN_PROGRESS` keepalives, or MF hits the inline budget, the dispatcher leaves the task in `ACCEPTED`/`IN_PROGRESS` and defers the next attempt until the heartbeat timeout expires. Once the watchdog fires, retries honour the declared strategy (fixed delay or exponential backoff).
- Sync failure (`sync` mode): long-running calls surface an error to the client the moment the timeout triggers. Because nothing was persisted, MF cannot resume the work after a crash; the client must resubmit using the same `wo_id`.
- Crash recovery: only queued (`async`) work orders survive a service restart. `sync` requests in flight are lost with the underlying HTTP connection.

### Task Schema
Each work order carries one or more tasks. MF persists the structure exactly as received:

```json
{
	"task_id": "d55c9314-a845-11f0-848a-c727ab9e1610",
	"task_name": "customer_payment",
	"endpoint": "https://bookkeeper/api/v1/customer-payment/d55c9314-a845-11f0-848a-c727ab9e1610",
	"method": "POST",
	"call_timeout_ms": 5000,
	"max_retries": 3,
	"retry": {
		"strategy": "exponential_backoff",
		"initial_delay_ms": 250,
		"multiplier": 2.0,
		"max_delay_ms": 10000,
		"max_attempts": 8
	},
	"heartbeat_timeout_ms": 5000,
	"completion_deadline_ms": 600000,
	"body": { "submission": "…" },
	"status_endpoint": "https://bookkeeper/api/v1/customer-payment/d55c9314-a845-11f0-848a-c727ab9e1610/status",
	"feedback_endpoint": "https://bookkeeper/api/v1/customer-payment/d55c9314-a845-11f0-848a-c727ab9e1610/feedback",
	"depends_on": []
}
```


Dependencies
-------------

- Each task can declare `"depends_on"` entries pointing to upstream task IDs. The optional `"on"` selector accepts `success` (default), `failure`, or `done`. Use `done` when the downstream step should run once the prior task reaches any terminal status, regardless of outcome.

**Retry strategy**

- `retry.strategy`: `fixed` (default) or `exponential_backoff`.
- `initial_delay_ms`: base delay before the first retry, defaulting to 100 ms when omitted.
- `multiplier`: exponential growth factor (ignored for `fixed`), minimum 1.0.
- `max_delay_ms`: optional ceiling for backoff delays.
- `max_attempts`: total attempts (initial call + retries). Defaults to 8; set to a positive value to tighten the guardrail or to a large number to allow more retries.
- `jitter_ms`: optional random jitter added per retry to reduce thundering-herd behaviour.
- When the `retry` block is omitted the dispatcher behaves like `fixed` with a 100 ms delay and a maximum of 8 attempts once the heartbeat window expires or a hard failure occurs.

**Downstream Callbacks**

- Work-order dispatch now embeds `meta.callback.status` with `http(s)://<mf>/work-orders/{wo_id}/tasks/{task_id}/status`. Bookkeeper (and any other worker) POSTs status documents to that endpoint so MF records the outcome immediately and re-queues any dependent tasks.
- Long-running tasks will eventually send periodic heartbeats by POSTing `status: "IN_PROGRESS"` with optional `details` (e.g., percentage complete). Each heartbeat extends MF’s watchdog so the dispatcher doesn’t retry while progress is reported.
- Cancellation/late-arrival feedback remains optional; once Bookkeeper stores task state in shared storage we can reintroduce a reliable `feedback_endpoint` contract. When a task omits `feedback_endpoint`, MF skips the callback entirely—useful for synchronous or otherwise trivial handlers where the downstream response already conveys the terminal state.

Responses emitted by MF (status queries) and downstream services (submit/status/feedback) always echo these identifiers inside a `meta` block so every consumer can correlate payloads without inspecting transport details:

```json
{
	"meta": {
		"wo_id": "f9c5b9d2-a845-11f0-8d51-9f9e4c0b1234",
		"task_id": "d55c9314-a845-11f0-848a-c727ab9e1610",
		"task_name": "health-check"
	},
	"status": "FINISHED",
	"attempt": 1,
	"response": { "result": "success" }
}
```

`GET /work-orders/{id}` returns the same shape and includes a `payload` field containing the original submission JSON for audit purposes.

The additional endpoints support MF’s recovery loop and audit feedback:
	- `status_endpoint`: polled during heartbeat recovery to learn whether the downstream service already finished the task (`200`), is still working (`202`), or never saw it (`404`).
	- `feedback_endpoint`: invoked by MF exactly once when a task reaches a terminal state (`FINISHED`, `FAILED`, `TIMED-OUT`, or any `LATE_*` event). Downstream services can use this callback to trigger compensations (e.g., voiding a late card capture).
	- `task_id`: client-supplied UUID; MF persists it as `BINARY(16)` alongside the work-order UUID so downstream services should treat it as an opaque identifier.

### Idempotency & Request IDs
	- `wo_id` is required for every submission; clients generate a UUID and reuse it for retries and status polling.
	- Replaying the same `wo_id` causes `sp_work_order_save` to no-op and MF returns the latest status instead of duplicating work.
	- Clients that lose the initial response can immediately poll `GET /work-orders/{wo_id}` or re-POST with the same identifier to recover the `submission_id`.
	- The API gateway can surface the idempotency key through a header (e.g., `Idempotency-Key`) while MF stores it as `wo_id`.

### Request Lifecycle
	1. Client sends request to API gateway.
	2. Gateway validates, enriches, and forwards to MF `POST /work-orders`.
	3. MF persists the work order (`QUEUED`), emits dispatcher events, and waits up to `sync_response_timeout_ms` for a terminal outcome.
	4. If the dispatcher receives a terminal status (`FINISHED` / `FAILED`) within the budget, MF returns that response to the client.
	5. If the downstream returns `202` (in-progress) or MF exceeds the timeout budget, MF responds `202 Accepted` with submission metadata and continues processing asynchronously.
	6. Client polls `GET /work-orders/{id}` for consolidated status and task history until completion.
	7. When the dispatcher resolves each task, it posts the final disposition to the task’s `feedback_endpoint` (if present) so the downstream service can reconcile its own state.

### Dispatcher Behaviour
	- Immediate execution: tasks are dispatched on virtual threads as soon as the work order is saved. HTTP 2xx responses finish the task; HTTP 202 transitions the task to `ACCEPTED` and defers to the heartbeat loop; other 4xx/5xx responses funnel through retry/backoff.
	- Deferred completion: the heartbeat runs every second, discovers tasks in `ACCEPTED`, and either resumes them immediately or, for stale `CALLING` rows, emits a `REQUEUED` event, applies jittered backoff, polls the `status_endpoint`, and only transitions back to `ACCEPTED` when the downstream still needs work. Successful probes mark the task `FINISHED`; client errors (`4xx`) convert to `FAILED`; a `404` indicates the downstream never saw the submission so MF retries the task.
	- Retry/backoff: task policies dictate `call_timeout_ms`, `max_retries`, retry `strategy`, `heartbeat_timeout_ms`, and `completion_deadline_ms`. MF records every transition in `tb_wo_task_event`, extending the heartbeat window whenever downstream keepalives arrive and declaring `TIMED-OUT` once the completion deadline passes.
	- Feedback: once a task reaches a terminal status, MF posts `{ meta: { wo_id, task_id, task_name }, status, response }` to the `feedback_endpoint`. Services that cannot act on the feedback simply omit the endpoint.

### API Surface Updates
	- `POST /work-orders` returns `202 Accepted` with `{ "submission_id": "...", "status": "ACCEPTED" }` when falling back to async, or the downstream response when completed inline.
- `GET /work-orders/{id}` continues to provide consolidated status and event history for clients.
- `GET /work-orders/{id}` now exposes `task_events`, allowing operators to see transitions such as `REQUEUED`, `RETRYING`, and any `LATE_*` states recorded after restart, and includes the original submission as `payload` alongside the correlation `meta` block.

### Downstream Service Contract
	- **Task endpoint** (`POST /api/v1/<task-name>/<task-id>`): invoked by MF with the task body. Must be idempotent; retryable errors should return 5xx, permanent failures 4xx. MF always supplies the `wo_id` query parameter and a `meta` block in the JSON body—downstream services should echo that block back verbatim in every response.
		- Downstream implementations **must** remain idempotent. If MF crashes after dispatch but before it records the callback, it will restart, detect the task stuck in `CALLING/ACCEPTED`, and submit it again. Duplication is expected; services must apply their work exactly once.
	- **Status endpoint** (`GET …/status`): returns
		- `200 OK` when the service is completely finished.
		- `202 Accepted` while work is still in progress.
		- `404 Not Found` if the service never observed the submission (MF will resubmit).
		- Other 4xx codes to signal unrecoverable errors; MF records `FAILED` and stops retrying.
	- **Feedback endpoint** (`POST …/feedback`): receives the final disposition (`FINISHED`, `FAILED`, `TIMED-OUT`, `LATE_SUCCESS`, etc.) once MF has committed the outcome. Services can use this callback to trigger compensations or clean up pending work. The `meta` block must be returned unchanged so MF can reconcile late acknowledgements.
	- **Idempotency**: downstream services must be able to handle duplicate calls safely; MF may resubmit after restart or network errors even if the status probe says “pending”.

### Recoverable Failures on the Wire
	- Downstream endpoints can signal that a failure is safe to retry by decorating their JSON response or callback payload. MF inspects two locations:
		- A top-level boolean flag: `"recoverable": true`.
		- Nested under an `error` object: `"error": { "message": "status text", "recoverable": true }`.
	- Example inline response:

	```json
	{
		"status": "FAILED",
		"error": {
			"message": "processor temporarily unavailable",
			"recoverable": true
		},
		"recoverable": true
	}
	```

	- Services only need to set the flag in one location. MF checks both the top level and the nested `error.recoverable` field for compatibility with existing integrations; either hint is treated the same.
	- When MF sees the recoverable flag it records the attempt for observability, keeps the work order in the queue, and schedules a retry according to the task policy instead of marking the task (and work order) as permanently failed.
	- Omit the flag—or set it to `false`—for permanent failures. MF will transition the task to `FAILED`, stop retrying, and update the work order status accordingly.

### Open Questions
	- Where to persist per-policy metadata (e.g., retry counts, deadlines) within the existing schema.
	- How MF surfaces completion notifications back to the API gateway or other interested services.
	- Strategy for horizontal scaling (shared database, sharded queues, or alternate backends).
