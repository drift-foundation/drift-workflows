# Microflows — documentation

**Microflows (MF)** is the job/submission controller that sits between API clients and
downstream domain services (Bookkeeper first). Clients submit work orders; MF persists,
enforces retry/timeout/idempotency policy, dispatches to downstream services over HTTP, and
reports status. Domain services stay thin: they execute idempotent work and surface state.

## Status / provenance
- This `microflows/` tree is part of a broader **lift of pushcoin-v3 components into this repo**
  as we adapt the stack to Drift-lang. Docs land first; the service follows.
- Microflows is **still implemented in Java** (see `pushcoin-v3/components/microflows`). A
  **Drift port is a planned future job**; this top-level `microflows/` tree is its new home in
  this repo. Today it holds documentation only.
- The three design docs here were **imported verbatim from
  `pushcoin-v3/components/microflows/docs/`** (each carries a provenance banner). They are the
  Microflows *design of record* — authoritative for MF's own behavior (policies, queue, retry,
  the client↔MF API), but they predate the Drift `bookkeeper` and describe some of the
  downstream contract aspirationally. **Where they differ from what `bookkeeper` actually
  serves today, this README wins.**
  - [`microflows_design.md`](microflows_design.md) — full MF service design: envelope, policy
    schema, task schema, dispatcher, retry/backoff, the downstream contract.
  - [`queue_design.md`](queue_design.md) — queueing strategy, sync-vs-async fallback, storage.
  - [`microflows_protocol_cheatsheet.md`](microflows_protocol_cheatsheet.md) — quick reference
    for states and the downstream contract.

## Two protocol surfaces
1. **Client / Gateway ↔ Microflows** — `POST /work-orders`, `GET /work-orders/{wo_id}`. Owned
   by MF; see the imported docs. Not implemented by bookkeeper.
2. **Microflows ↔ Bookkeeper (downstream task protocol)** — the contract **bookkeeper
   implements**, and the focus of the rest of this README.

---

## The MF ↔ Bookkeeper task protocol (as bookkeeper implements it *today*)

MF is the controller; bookkeeper is the downstream **task executor**. MF dispatches a task over
HTTP, bookkeeper claims it (idempotently), works it asynchronously, and signals completion via
**two independent channels**: a **callback** (primary) and a **status query** (fallback).

### Endpoints bookkeeper exposes
Route group `/api/v1/tasks`, with task-key path params `{workOrderId}/{taskName}/{taskId}`:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/tasks/{workOrderId}/{taskName}/{taskId}` | Submit/execute the task (idempotent). |
| `GET`  | `/api/v1/tasks/{workOrderId}/{taskName}/{taskId}/status` | Query task state. |
| `POST` | `/api/v1/tasks/{workOrderId}/{taskName}/{taskId}/feedback` | MF→bookkeeper terminal feedback. |
| `GET`  | `/health` | Liveness (outside the task group; no auth/path-params). |

`task_name` selects the handler (`dispatch_task`): e.g. `health-check`, `customers-snapshot`,
and the new `microflow-proto-check` (below). An unknown `task_name` → `FAILED_UNKNOWN_TASK` (404).

### Request body
```json
{
  "account_id": "11111111-2222-3333-4444-555566667777",
  "meta": { "callback": { "status": "http(s)://<mf-or-test>/.../status" } }
}
```
- `meta.callback.status` is the **callback URL** bookkeeper POSTs the terminal envelope to.
  Omit/empty → callback is skipped.
- The rest of the body is task-specific (e.g. `account_id` for `customers-snapshot`).

### Submit response & state mapping
The POST is **idempotent** and returns the current task state, mapped to HTTP:

| Task state | HTTP | Meaning |
|---|---|---|
| `IN_PROGRESS` | `202` | Claimed; a worker is producing asynchronously (a *pending* envelope is returned immediately — the POST does **not** block on the work). |
| `FINISHED` | `200` | Already complete; served from the terminal record (no rebuild). |
| `FAILED_INVALID_PAYLOAD` | `400` | e.g. empty body. |
| `FAILED_UNKNOWN_TASK` | `404` | No handler for `task_name`. |
| `FAILED` | `422` | Terminal (permanent) failure — unrecoverable; 4xx so a controller stops retrying (recoverable failures never surface here — they stay pending). |
| (unexpected) | `500` | — |

### Idempotency
Backed by **Singular** (the internal idempotency store), keyed by `service_group` + a derived
`snapshot_key` (for `customers-snapshot`: `account_id` + task + snapshot-hour). A replay returns
the existing terminal record rather than re-running. Terminal records are immutable.

### Completion: two channels, opposite directions
- **Callback (primary, egress from bookkeeper):** on a terminal state the worker POSTs the
  disposition envelope (`{ status: "FINISHED"|"FAILED", ... }`) to `meta.callback.status`. This
  is how MF normally learns of completion.
- **`GET …/status` (fallback, inbound to bookkeeper):** for an **impatient controller** —
  "I should have had a callback by now; let me ask directly." Per the v3 contract this returns
  `200` finished / `202` in-progress / `404` never-saw / `4xx` unrecoverable.

Because the two channels travel **opposite directions**, together they localize connectivity
faults (see the deploy gate below).

---

## Divergences from the imported v3 design (current `bookkeeper`)
1. **Path shape.** `microflows_design.md` shows `POST /api/v1/<task-name>/<task-id>`; bookkeeper
   actually serves the **grouped** form `/api/v1/tasks/{workOrderId}/{taskName}/{taskId}`
   (`+/status`, `+/feedback`).
2. **`GET …/status` — real for path-keyed tasks, delegated for business-keyed tasks.** For
   `microflow-proto-check` (keyed by the HTTP path) `handle_status` inspects the Singular
   `(service_group, key)` record (read-only) and reports **real** state: `IN_PROGRESS` (202) /
   `FINISHED` (200, +metadata) / `FAILED` (**422**, unrecoverable — `404` is reserved for
   "never saw"/resubmit) / `UNKNOWN` (404). For `customers-snapshot` the Singular key is a
   *business* key (`account_id` + hour) not derivable from a path-only GET, so its status stays
   **delegated** — `200 {"status":"DELEGATED", …}`, completion via callback. (The pushcoin-v3
   Java bridge had a path-keyed `TaskStateStore`; restoring real status for the business-keyed
   handlers is a separate reconciliation.)
3. **Idempotency home.** v3 leaned on MF-side `wo_id`; bookkeeper additionally enforces it in
   **Singular** (immutable terminal records), so re-POST/replay is safe at the executor too.

---

## Our additions

### `microflow-proto-check` — protocol conformance task
A dedicated synthetic task (`task_name = "microflow-proto-check"`) whose only purpose is to
exercise the full MF↔bookkeeper protocol — claim → (optional delay) → produce a trivial
deterministic result → callback + `GET …/status` — **without any real business work**. Think
"async `/health`." Its timing/behavior knobs are **first-class inputs** (not gated test-hooks),
so it's benign and prod-safe:
- `delay_ms` — sleep after claim before completing → makes the `IN_PROGRESS` window observable.
  Currently **unbounded**; a cap to bound a slow-loris/DoS (many long-sleeping workers) is a
  deferred hardening.
- `suppress_callback` — complete (`FINISHED`) but post **no** callback → models "no callback
  came," forcing the controller to fall back to `GET …/status`.
- `callback_delay_ms` *(optional)* — delay the callback so it races a status poll that already
  saw `FINISHED`.
- `fail` — `recoverable` or `permanent`, drive the failure path. **Permanent** (`gw.fail`
  non-retryable) → Singular `STATUS_FAILED` → `GET /status` reports `FAILED`, and a re-POST is an
  idempotent FAILED replay. **Recoverable** (retryable) → Singular re-queues as `PENDING` →
  `GET /status` reports `IN_PROGRESS` (a retryable failure *is* "will be retried"); the
  `FAILED`+`recoverable` signal travels via the **callback**, not `GET /status`.
- `emit_artifact` — when set, write a **dummy output zip** to the output-store. Lets the deploy
  smoke also validate **CDN reachability + write permissions**, not just the protocol round-trip.
  Off by default (pure protocol check).

Boolean toggles (`suppress_callback`, `emit_artifact`) accept a real JSON boolean
(`true`/`false`); an integer `0`/`1` is also tolerated. `delay_ms`/`callback_delay_ms` are
integers (ms); `fail` is a string.

**Prod exposure:** the endpoint is **registered in prod** — required so the deploy gate can
verify the *real* instance's bidirectional connectivity (inbound POST/GET *and* callback egress)
against its actual firewall/routing. Safe because the handler does no business work and touches
no customer data. Only abuse vector is `delay_ms` (piling up sleeping workers); left uncapped for
now, bound later.

### Deploy / smoke gate
Every deployment must pass an **inline `microflow-proto-check`**: `POST` → `202` → **require the
callback at the receiver within T** → `GET …/status` = `FINISHED`. Set `emit_artifact` for the
"full" smoke to also assert a dummy zip landed in the output-store — validating CDN reachability
+ write permissions. Because callback (egress) and
`GET …/status` (inbound) go opposite ways, the gate pinpoints the broken leg:
- callback missing **but** status `FINISHED` → **callback egress / firewall blocked** (the silent
  killer: real tasks `202` fine but never notify).
- POST ok but status unreachable → inbound/routing problem.
- POST fails → listener/port down.
A single firewall rule can break callbacks or a port; this gate catches it at deploy time.

### HTTP test driver
Python (`pytest` + `requests`) black-box driver that spawns `bookkeeper` **under valgrind**,
hosts a **callback sink** (we play the role of MF being called back), and asserts the
*(synchronous response + async callback + valgrind-clean)* triple — with `microflow-proto-check`
as its primary scenario. Full design: [`../../work/bookkeeper-http-test-driver.md`](../../work/bookkeeper-http-test-driver.md).
