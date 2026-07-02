# Run the starter kit locally

Drive the example workflows through `uflowsd` over HTTP. This is the same path your
application will use: submit a named script, resume pending work, inspect outcomes, redeploy by reload.

> Implementing a **participant service** or authoring a **manifest**? See
> [`../doc/uflowsd_participant_contract.md`](../doc/uflowsd_participant_contract.md) — the as-built,
> conformance-pinned contract (manifest schema, `.mf` op naming, the participant PUT/GET wire protocol).

## 0. Prerequisites

- The certified toolchain (driftc 0.33.53 / abi 18) and a built `uflowsd` binary
  (`microflows/runner` builds artifacts `mfrunner` and `uflowsd`).
- A MariaDB with the Microflows schema loaded, and your participant services reachable.
- Edit `manifest.json`: set `deployment.db.*` to your database and each participant's
  `transport.endpoints` to your services. (For a pure local smoke test, point all three logical
  participants at one stub that implements the operations — that is exactly what the integration gate
  does.)

## 1. Start the service

```bash
uflowsd --manifest /path/to/microflows/examples/manifest.json --port 8088
```

At startup the service **loads and validates every script** in the manifest over the shared
deployment (lowers each `.mf`, builds + validates its plan, checks every non-final operation has a
compensation). A bad manifest fails fast — the service refuses to start. Script `path`s are resolved
relative to the manifest file.

Health for load balancers:

```bash
curl -s localhost:8088/healthz   # liveness  -> {"status":"ok"}
curl -s localhost:8088/readyz    # readiness -> 200 {"status":"ready"} while accepting; 503 when draining
```

## 2. Submit a workflow

A workflow instance is identified by a **caller-chosen 32-hex id** (use a UUID without dashes). The
request body is the instance arguments; `?script=` names which deployed script to run.

```bash
WF=$(uuidgen | tr -d -)
curl -s -X POST "localhost:8088/v1/workflows/$WF/submit?script=payment_authorize_capture" \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"ord-1001","customer_id":"cust-42","amount":{"value":1299,"currency":"USD"}}'
# -> {"workflow":"completed","operation_id":"…","result":{"ledger_entry_id":"…"}}   (HTTP 200)
```

The response body is the **outcome document** (identical to the CLI's); the HTTP status is semantic:

| Outcome | HTTP | Meaning |
|---|---|---|
| `completed` / `already_terminal` | 200 | success terminal |
| `failed` (`compensated` true/false) | 200 | terminal failure; body carries `reason` + `compensated`; exit 3 |
| `pending` / `deferred` / `pending_restart` | 202 / 503 | in flight; retry / drain back-pressure |
| `refused` (draining) | 503 | not accepting new work |
| `aborted` (invalid args / unknown script / malformed body) | 400 | client error, no workflow created |
| `blocked` (`direction` forward/reverse) | 409 | manual resolution required (e.g. a persistent participant route-404 that exhausted the reconcile budget); body carries `direction` + `reason`; exit 3 |
| `not_found` | 404 | no such workflow |

## 3. Resume pending work

If a participant is momentarily unavailable or returns "pending", the workflow defers. Re-drive it by
**resuming the same id** (no script, no body — it runs strictly by the durable pin). A participant `404`
(no record of the operation) is reconciled — Microflows safely re-submits the identical request — and a
*persistent* route-404 is bounded by the **reconcile budget**: it keeps deferring within budget, then
enters `blocked` (direction forward, or reverse if a compensation is the one 404ing) for manual
resolution, never an infinite silent pending. Tune it per deployment (optional;
defaults 30 min / 2 attempts):

```json
"deployment": { "...": "...", "reconcile_budget": { "max_elapsed_ms": 1800000, "min_attempts": 2 } }
```

`max_elapsed_ms` is the wall-time bound (integer > 0); `min_attempts` a small floor (integer ≥ 1) so one
404 plus clock skew can't block. A malformed `reconcile_budget` is rejected at startup, never silently
defaulted.

```bash
curl -s -X POST "localhost:8088/v1/workflows/$WF/resume"
```

Resume is idempotent: a completed workflow replays its terminal result from durable state (**no
participant call** — works even if the participant is down); an in-flight one continues.

## 4. Compensation (automatic)

Submit `account_adjustment_with_rollback` and arrange for `post_journal` to fail (in the gate this is
an out-of-band stub control). `adjust_balance` succeeds, `post_journal` fails, and the service unwinds:

```
-> {"workflow":"failed","reason":"participant_invalid_request","compensated":true}   # reverse_adjustment ran against the durable adjustment checkpoint
```

No reversal code is written in the workflow — the `compensation` binding in the deployment drives it.

## 4b. Result-conditional branch + authored `fail` (business decline)

`payment_decline_guard` shows the workflow — not the participant — deciding what a gateway result
means. `authorize` returns a **200** carrying its decision (`status`); the workflow branches on it:

```
# decision "approved" -> capture, complete
-> {"workflow":"completed","operation_id":"…","result":{"capture_id":"…"}}   (HTTP 200)

# decision "declined" -> the `fail "payment_declined"` branch runs: the authorization is voided
# (compensation) and the instance terminates as a durable, compensated failure
-> {"workflow":"failed","reason":"payment_declined","compensated":true}      (HTTP 200, exit 3)
```

A 200 is a valid *result*, not a transport success — the `.mf` expresses the decline-then-unwind
policy with `case result authorization.status { … "declined" { fail "payment_declined" } … }`. With
no compensable step yet settled, a `fail` terminates as `{"…":"failed",…,"compensated":false}`.


## 5. Reload a new manifest (zero-downtime redeploy)

Edit `manifest.json` (add/replace scripts, bump versions), then signal the running service:

```bash
kill -USR1 <pid>     # staged reload: validate the new manifest into a standby, then atomically swap
```

If the new manifest is invalid, the reload is **rejected** and the service keeps serving the old one.
Workflows already created continue by their **durable pin** — a reload never breaks in-flight or
completed workflows.

## 6. Graceful shutdown / drain

```bash
kill -TERM <pid>     # admission -> draining: new submissions get 503; in-flight work converges; then exit
```

---

## Performance note

Each participant dispatch is one HTTP round-trip; a workflow's latency is roughly the sum of its
operations' participant latencies. (An earlier `web.rest` keep-alive defect that added ~2.3s per
dispatch was fixed in web-rest 0.5.6 / driftc 0.33.53.)

---

## Security boundary — intentionally deferred

**This kit is "prod-shaped workflows + service usage", not "production security complete."** Every
participant uses `auth_profile: null` and the HTTP API is **internal only** — there is no authentication,
authorization, or caller identity on it yet. That is deliberate: we are **not** guessing the security
model. The `/v1/workflows` route group is the seam where it will attach (see `microflows_design.md`
§15.4), but no auth logic is built.

Before we design it, we want concrete requirements from the business app team on:

1. **Who submits workflows** — which services/users call the API, and how they are identified.
2. **How user / session / security context should be represented** — what travels with a submission and
   how it is carried through to participants.
3. **What participant credentials need to look like** — how the coordinator authenticates to each
   participant (`auth_profile`'s real shape).
4. **What audit fields are required** — who/when/why, and where they must be recorded durably.
5. **How revocation / denial during delayed phases should behave** — if authorization is revoked while a
   workflow is pending/deferred, what should happen to the in-flight work.

We will design the security model against these answers rather than speculation. Until then: keep the
service on a trusted internal network, in front of your own application's authenticated front door.
