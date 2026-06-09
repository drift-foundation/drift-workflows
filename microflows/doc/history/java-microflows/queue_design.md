> **Imported from `pushcoin-v3/components/microflows/docs/` — the canonical Microflows (Java) design of record.**
> Microflows is still implemented in Java; a Drift port is a planned future job, and this
> `microflows/` tree is its new home in this repo. This file is preserved as-is for design intent.
> For how the protocol is **actually implemented today** by the Drift `bookkeeper` (and where it
> diverges), plus the `microflow-proto-check` conformance task and the deploy gate, see
> [README.md](README.md).

# Microflows Queueing Strategy

## Goals
- Shield API clients from backend instability by always accepting submissions (`ACCEPTED` vs `FINISHED`) and completing the work later when downstream services recover.
- Keep domain services (Bookkeeper first) lean: they receive HTTP requests from Microflows, are agnostic to retries or storage, and simply process idempotent work items.
- Support synchronous responses for trivial workloads while gracefully falling back to queued execution when a request exceeds the inline budget or the system is under stress.
- Enforce idempotency by requiring the client to supply a `request_id` (`UUID`) for every meaningful call -- including heavy GETs -- so Microflows can detect resubmissions after connection drops.
- Decouple queue persistence from the dispatcher. The v1 implementation can continue to use MariaDB, but the design must allow a future filesystem queue without touching service contracts.

## End-to-End Flow
1. **Web client** keeps retrying the API call (automated or via a Retry button) until it receives `ACCEPTED` or `FINISHED`. When it receives `ACCEPTED`, it stores the returned `submission_id` for subsequent status checks.
2. **API Gateway** authenticates the caller, enriches the request (`account_id`, `user_id`, other auth metadata), validates the payload, and forwards it to Microflows.
3. **Microflows (MF)** persists the submission (when operating in async mode), enforces policy (retry, deadlines, load balancing), and routes the request to Bookkeeper via HTTP. It also exposes `POST /work-orders` and `GET /work-orders/{id}` for clients to submit and poll.
4. **Bookkeeper** exposes two endpoints per task: the task endpoint (`POST .../task`) that performs the work, and a status endpoint (`GET .../status`) so MF can check progress. Bookkeeper never knows if a request is a retry; it simply performs idempotent work based on the `request_id`.

## Request Contract
Every submission contains:

```json
{
	"request_id": "client-generated-uuid",
	"task_name": "ledger.payment",
	"mode": "async",               // optional; default async
	"sync_response_timeout_ms": 750, // inline budget before MF returns 202
	"meta": {
		"account_id": "district-42",
		"user_id": "parent-1001",
		"source": "web",
		"auth": { "scopes": ["wallet:write"] }
	},
	"payload": { /* domain body forwarded to Bookkeeper */ }
}
```

Key semantics:
- `request_id` remains stable across all retries. GETs that can be expensive must also include one; if the client loses the HTTP connection during a long-running GET, it can resubmit or poll without re-running the underlying report.
- `mode` governs behaviour:
	- `sync`: MF keeps everything in memory, waits for Bookkeeper to finish, and returns the downstream response. Nothing is queued, so a crash mid-flight requires the client to retry.
	- `async` (default): MF tries to finish inline using the provided `sync_response_timeout_ms`. When the budget expires -- or Bookkeeper responds with an in-progress status -- MF persists the submission and immediately returns `202 ACCEPTED` with `{ "submission_id": "...", "status": "ACCEPTED" }`.
- `meta` is opaque to MF; it persists the blob for auditing and includes it in feedback callbacks.

## Status Semantics
- `POST /work-orders` responses:
	- `200 OK` / `201 Created`: request completed entirely inline (rare).
	- `202 Accepted`: work has been persisted or will continue asynchronously. The payload includes `submission_id`, `status`, and echoes `request_id`.
- `GET /work-orders/{submission_id}` returns:
	- Aggregated task statuses.
	- The original `payload` + `meta`.
	- `events` showing transitions (`CLAIMED`, `REQUEUED`, `FAILED`, `TIMED_OUT`, etc.).

GET requests that opt into async semantics follow the same lifecycle: the gateway forwards them to MF, MF blocks until either the data is ready or the inline budget expires, and then MF falls back to async with `202 ACCEPTED`. Polling the `GET /work-orders/{id}` endpoint eventually returns the full report.

## Storage Strategy
- **Current**: reuse the existing MariaDB schema (`uflows`) for persistence. MF already writes work orders and task events there, so the dispatcher logic keeps functioning while we refactor the API layer.
- **Abstraction**: define a storage interface (e.g., `WorkOrderStore`) so the dispatcher does not depend on MariaDB specifics. When we revisit filesystem queues, only the interface implementation changes.
- **SQL artefacts**: remain under `bootstrap/db/pc3` so Mariachi (future tool) can propagate them to every tenant. Until then, Skeema keeps the schema aligned.

## Idempotency & Retries
- The client must reuse `request_id` for every retry (manual or automated) to prevent duplicate debits.
- Bookkeeper treats `request_id` as the natural primary key and must be idempotent: if MF retries after a crash, Bookkeeper either returns the cached result (`ALREADY_DONE`) or continues processing without starting a duplicate ledger transaction.
- MF stores `request_id` as `BINARY(16)` and enforces uniqueness per `account_id` + `task_name` to avoid collisions where two parents accidentally reuse the same UUID.

## Policy Handling
- Policy schema lives alongside the submission body (see `microflows_design.md`). `sync_response_timeout_ms`, `retry.strategy`, `heartbeat_timeout_ms`, etc., control when MF switches from sync to async and how it schedules retries.
- Policies also describe how MF should probe Bookkeeper: if a task exposes a `status_endpoint`, MF polls it during the heartbeat window; if not, MF simply retries the task per the backoff schedule.

## Testing Approach
- Use **pytest + httpx.AsyncClient** to drive the app in-memory. No uvicorn dependency is required; the ASGI/Starlette app can be tested directly, mirroring our async production runtime.
- Base test command (already wired in the microflows justfile): `just test-all` -> `pytest tests`. Extend it with scenario suites:
	1. `test_sync_success.py`: sanity check for quick inline completions and enforcing `202` for medium requests.
	2. `test_async_fallback.py`: force Bookkeeper to delay responses so MF returns `202` and persists the work.
	3. `test_retry_policies.py`: simulate recoverable vs permanent errors.
	4. `test_get_async.py`: confirm heavy GETs honour `request_id` idempotency and behave like POSTs.
- Each test suite reuses the shared DB bootstrap logic (same as GlTestRunner) so we start from a clean schema before running assertions.

## Proposed Initial Implementation Task
1. **HTTP contract**: Implement `POST /work-orders` + `GET /work-orders/{id}` inside Microflows using the policy/idempotency rules above.
2. **Persistence shim**: Introduce a facade around the existing MariaDB tables (`WorkOrderStore`) so we can reuse it today and swap the backend later.
3. **Bookkeeper stub**: add a lightweight in-process fake that mimics Bookkeeper's HTTP API (success, `202`, delayed completion). Tests will depend on it.
4. **Test coverage**: add `tests/integration/test_sync_success.py` verifying:
	- Inline completion under light load returns `200`.
	- Requests that exceed `sync_response_timeout_ms` return `202` with `submission_id`.
	- Re-submitting the same `request_id` returns the cached status rather than duplicating work.

With these foundations, we can iteratively plug in real storage (MariaDB now, filesystem later), broaden Bookkeeper integration, and expand the policy matrix without redesigning the API again.
