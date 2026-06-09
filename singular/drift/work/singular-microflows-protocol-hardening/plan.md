# Singular / Microflows Protocol Hardening - Review Plan

**Status:** draft for team review; no implementation started.
**Audience:** PushCoin, Microflows, Bookkeeper, and Singular owners.
**Date:** 2026-06-03.

## Summary
Harden the current task protocol so side effects are **effectively once**:

- A task may be attempted more than once.
- Every irreversible downstream operation must use a stable operation-level idempotency key.
- Singular must accept, store, and enforce an opaque app-minted lease token so stale workers cannot commit terminal state or notify success.
- Singular must be the authoritative source for lease expiry and expired-lease recovery attempt count.

This is protocol hardening only. It does not implement the full sale workflow. The first goal is to make the existing Singular-backed task controller semantics precise enough that bookkeeper can safely execute long-running or retried work.

## Why
The current protocol is close to an idempotent async task pattern, but there are edge cases that can still produce duplicate work or misleading callbacks:

1. A duplicate submit can look like a fresh claim when the same `lease_owner` already owns a `WORKING` item.
2. A stable worker/node identity is not enough authority. Reusing the same identity can accidentally re-enter work that is already active.
3. A long-running worker can lose its lease, keep doing side effects, and later try to complete.
4. A stale worker can still send a terminal callback if callback emission is not fenced by the Singular terminal write result.
5. Microflows can timeout and resubmit, but its local retry count is advisory. In a networked system, only Singular atomically knows how many expired-lease recovery attempts were actually granted for a key.
6. Singular can prevent duplicate terminal records, but it cannot by itself prevent duplicate external effects such as card charges. Those effects need their own stable provider idempotency keys.

The intended guarantee is therefore **at-least-once attempts, effectively-once effects through idempotency and fencing**.

## Target Guarantee
For a given task key:

- Singular is the internal coordination and terminal-state source of truth.
- Bookkeeper starts work only after Singular returns a fresh or reclaimed `WorkLease`.
- Duplicate submits while work is active return `IN_PROGRESS` and do not spawn duplicate workers.
- Workers renew leases while performing work that may exceed the lease timeout.
- Workers stop before further side effects if renewal proves their lease token is no longer current.
- Terminal callbacks are sent only by the worker that successfully recorded, or validly replayed, the terminal Singular result under the current lease token.
- External systems that perform irreversible operations receive stable idempotency keys derived from the logical operation, not from a worker, node, process, or lease attempt.
- Microflows retry counts and worker identities are best effort. Singular's lease expiry and expired-lease recovery attempt count are authoritative.

## Layering Boundary
Singular and Microflows remain independent frameworks. This plan discusses both only because the end-to-end guarantee depends on both layers.

Singular responsibilities:

- Serialize work by stable key `X`.
- Accept and fence app-minted lease tokens.
- Track lease expiry and expired-lease recovery attempt count.
- Store opaque JSON progress/checkpoint payloads.
- Accept terminal state once.

Microflows protocol responsibilities:

- Schedule and resubmit work.
- Issue an opaque dispatch sequence/correlation token per dispatch.
- Require that token to be echoed in synchronous responses and callbacks.
- Ignore stale or out-of-sequence callbacks.

Singular docs should not mention Microflows. Microflows docs should not need to know about Singular. Each layer can provide its own correctness boundary; this plan records how they compose in PushCoin.

## Durable Step Key
Microflows chooses the stable step transaction ID, `X`. Singular already accepts this as the caller-supplied `idempotency_key`.

For any irreversible logical step:

- `X` is created early and is stable across all retries/reclaims/resubmits.
- `X` has one terminal state: `SUCCESS` or `FAILED`.
- Every worker attempt for that step operates on `X`.
- External processor idempotency keys are derived from `X`.
- A retry/reclaim is not new logical work. It is another attempt to drive `X` to terminal state.

For a card charge, a worker must use `X` to decide whether to submit, reconcile, poll, defer, or finish:

- If the processor reports `X` completed, record `SUCCESS` for `X` and return completion to Microflows.
- If the processor reports `X` failed, record or surface failure according to task policy.
- If the processor reports `X` pending, record non-terminal progress in Singular and defer/resume later.
- If the processor reports an indeterminate/unknown status for `X`, terminally park `X` as `INDETERMINATE` and raise an alarm; do not retry or fail automatically.
- If the processor has no record of `X`, retry the charge using the same provider idempotency key derived from `X`.

## Non-Terminal Progress
Singular should allow a worker that holds the current `WorkLease` to record durable non-terminal progress for `X`.

This is not processor-specific logic inside Singular. Singular stores opaque task-owned checkpoint/progress JSON and controls who may update it. Singular should validate only that the payload is a valid JSON object (the document contract); the empty document is `{}`. The task/worker owns the checkpoint schema, meaning, and versioning.

Suggested operation:

```text
defer(WorkLease, checkpoint_json, not_before)
```

or equivalently:

```text
checkpoint(WorkLease, checkpoint_json, next_state = Waiting, not_before)
```

Semantics:

- The update requires the current `lease_token`.
- Singular records the task-owned checkpoint/progress payload.
- Singular marks `X` as non-terminal waiting/pending, optionally with `not_before`.
- Singular clears or invalidates the active lease token so the worker can quit cleanly.
- A later `resume()` for `X` returns a new `WorkLease` plus the checkpoint, and the worker resumes from that context.

Example:

1. Worker A claims `X = card charge`, receives `T1`.
2. Worker A submits to the processor with provider idempotency key derived from `X`.
3. Processor replies: "received, check back in 1 hour."
4. Worker A records:

```json
{
  "phase": "processor_pending",
  "provider_ref": "...",
  "idempotency_key": "X"
}
```

with `not_before = now + 1 hour`.
5. Worker A exits. Microflows can check/resubmit later.
6. Worker B later claims `X`, receives `T2` and the checkpoint, then polls/reconciles the processor instead of submitting a fresh charge.

The key rule: the resumer interprets the checkpoint and acts accordingly; Singular only validates JSON, stores it, and fences updates.

## Work Item States
Use explicit state names so claimability is unambiguous.

Proposed states:

| State | Meaning | Claim behavior |
|---|---|---|
| `WORKING` | Actively leased to a worker. | `start()` / `resume()` returns active metadata while lease is valid; if expired, `resume()` may reclaim and grant a new `WorkLease`. |
| `DEFERRED` | Not terminal, not actively owned, intentionally parked until `not_before`. | `resume()` grants no lease before `not_before`; at/after `not_before`, `resume()` may grant a resume `WorkLease`. |
| `DONE` | Terminal success. | Terminal replay. |
| `FAILED` | Terminal failure. | Terminal replay. |
| `INDETERMINATE` | Terminal alarm state: outcome cannot be safely classified as success or failure, and automatic retry may duplicate an irreversible effect. | Terminal replay; no automatic claim. |

`PENDING` is too ambiguous for the hardened protocol. `READY` is also unnecessary: fresh work is represented by no existing row, and claimable existing work is `DEFERRED` with `not_before <= now`.

Start/resume transitions:

- `start()` on new item -> `Granted(Fresh, WorkLease)`.
- `start()` on existing item -> non-grant outcome; no resume/reclaim logic.
- `resume()` on missing item -> `NotFound` / protocol error.
- `resume()` on `WORKING` with valid lease -> `Active`, no `WorkLease`.
- `resume()` on `WORKING` with expired lease -> `Granted(Reclaim, WorkLease)`.
- `resume()` on `DEFERRED` before `not_before` -> `Deferred`, no `WorkLease`.
- `resume()` on `DEFERRED` at/after `not_before` -> `Granted(Resume, WorkLease)`.
- `resume()` on `DONE` / `FAILED` / `INDETERMINATE` -> `Terminal(TerminalResult)`.

Retryable/non-terminal outcomes should move to `DEFERRED`, not terminal `FAILED`. Immediate retry is represented as `DEFERRED` with `not_before <= now` or a very short future delay. A known unrecoverable outcome can move to terminal `FAILED` from any current `WorkLease`.

`INDETERMINATE` is for the worst unsafe external state: for example, a processor says "unknown status for X" and the worker cannot know whether a charge happened. Automation must not charge again, and it must not falsely fail the step. Mark `X` terminal `INDETERMINATE` with task-owned JSON context so monitoring can alarm.

First-pass decision: `INDETERMINATE` is immutable terminal in Singular. It does not later rewrite to `DONE` or `FAILED` through normal `complete()` / `fail()`. Remediation happens through a separate business/ops job in reference to `X`, with its own idempotency key and audit trail. That remediation job may automate processor follow-up or route to manual review, but it does not mutate the original terminal `X`.

Singular is passive: it does not wake itself up. In PushCoin, Microflows is the claim driver: it keeps dispatching/checking until it receives a terminal state. A `Deferred` outcome must include `not_before` so bookkeeper can tell Microflows when to resume without exposing Singular internals.

## Lease Token Model
Singular should distinguish descriptive identity from mutation authority:

- `idempotency_key`: the stable step key, `X`.
- `lease_owner`: descriptive worker/process identity for logs and debugging.
- `lease_token`: opaque app-minted capability/fence token required to mutate `X`.

The key rule: knowing `X` and `lease_owner` is not enough to mutate state. A worker must hold the current `lease_token`.

Claim flow:

1. Worker/app generates a 16-byte lease token using application-layer crypto.
2. First dispatch calls `start(X, proposed_token, item_meta, lease_meta, lease_timeout_seconds, claim_policy)`.
3. `start()` uses primary-key insert serialization; exactly one caller creates `X`, stores the proposed token, initializes the recovery attempt count to `1`, and returns `WorkLease(X, token, kind = Fresh, recovery_attempt = 1, lease_expires_at = ...)`.
4. Later dispatches call `resume(X, proposed_token, lease_meta, lease_timeout_seconds, claim_policy)`.
5. If `X` is already `WORKING` and unexpired, Singular returns `Active` metadata and no `WorkLease`.
6. If `X` is `DEFERRED` and `not_before` has arrived, Singular stores the proposed token and returns `WorkLease(X, token, kind = Resume, recovery_attempt = current, checkpoint = ..., lease_expires_at = ...)`. Planned resume does not increment the recovery attempt count.
7. If `X` is `WORKING` but expired, Singular stores the proposed token, fences the old token, increments the recovery attempt count, and returns `WorkLease(X, token, kind = Reclaim, recovery_attempt = n, checkpoint = ..., lease_expires_at = ...)`.
8. Once the new token is current, stale token `T1` cannot `renew`, `complete`, `fail`, or update checkpoint.

Mutation flow:

- `renew`, `complete`, `fail`, and checkpoint updates require the current `lease_token`.
- Reclaim accepts and stores a new app-minted `lease_token` and fences all previous tokens.
- Renew may either extend the current token or rotate to a new token. Token rotation on renew is stronger, but rotating on reclaim and requiring the current token for terminal mutation is the minimum hardening.

This turns ownership into a capability. Code that receives `Active`, `Deferred`, `Terminal`, or `NotFound` has no `WorkLease` to pass into worker execution or terminal mutation.

## Authoritative Recovery Attempt Count
Singular should atomically track the number of expired-lease recovery attempts for `X`.

Important distinction:

- Singular recovery attempt count = the fresh start plus expired-`WORKING` reclaims for `X`.
- Processor attempt count = number of irreversible processor requests made for `X`.
- Planned resume count = number of `DEFERRED` resumes for `X`; this does not consume the recovery budget.
- Microflows retry count = orchestration resubmits/timeouts, best effort only.

Only Singular can authoritatively count expired-lease recovery attempts. Microflows can provide retry policy, but its local count must not be trusted as fact.

Suggested policy shape:

```text
start(X, proposed_lease_token, lease_timeout_seconds, max_recovery_attempts)
resume(X, proposed_lease_token, lease_timeout_seconds, max_recovery_attempts)
```

Inside the claim transaction:

- If `X` is new, grant recovery attempt `1`.
- If `X` is `DEFERRED` and due, grant `Resume` without incrementing recovery attempts.
- If `X` is `WORKING` and expired, and `recovery_attempt_count < max_recovery_attempts`, grant `Reclaim` with `recovery_attempt_count + 1`.
- If the reclaim grants the last allowed recovery attempt, return that fact in the `WorkLease` metadata, for example `is_final_recovery_attempt = true`.
- If `X` is expired/non-terminal and `recovery_attempt_count >= max_recovery_attempts`, atomically transition `X` to terminal `FAILED(attempts_exhausted)` and return terminal failure metadata.
- Active duplicate claims do not increment recovery attempt count.

`max_recovery_attempts` may be passed as claim policy or stored in `item_meta`; the key point is that Singular compares it against the atomic count.

Attempt exhaustion is a cleanup/finalization path after all allowed recovery attempts were already granted and no worker produced a terminal result. It is not the normal way an attempt reports failure. Any worker holding the current `WorkLease` may call `fail()` on any attempt when the task-specific result is known terminal, such as a permanent card decline or bad CVV.

Planned slow work should be bounded separately by a task-owned overall step deadline or `not_after`/expiration policy. Do not use the recovery attempt budget to limit legitimate `DEFERRED` polling cycles.

Example: if a processor repeatedly responds "received, check back in 1 hour", each cycle should record `DEFERRED` progress and later `Resume` without incrementing recovery attempts. It becomes terminal `FAILED` only when task policy says the overall step deadline or poll budget is exhausted, not because the recovery attempt budget was consumed.

Because Singular has no scheduler by design, exhaustion is claim-driven. In PushCoin, Microflows must continue driving dispatch/status checks until `X` is terminal. Otherwise a non-terminal `DEFERRED` or expired `WORKING` item can remain unresolved indefinitely.

## Task Retry And Backoff Budget
Do not overload Singular's recovery attempt count for task-level transient failures.

Two independent budgets exist:

- Singular recovery budget: counts fresh start plus expired-`WORKING` reclaims. This is business-agnostic and protects against dead/stale workers.
- Task retry/backoff budget: stored in task-owned checkpoint JSON. This counts processor connect failures, provider transient errors, poll attempts, and similar business/backend retry policy.

Example: a worker tries to connect to the processor 5 times with 5-second backoffs while holding a valid `WorkLease`. All 5 failures happen inside one Singular lease. The worker decides immediate retry is wasteful, so it records:

```json
{
  "phase": "processor_unreachable",
  "checkpoint_version": 1,
  "connect_fail_count": 5,
  "last_error": "tcp_connect_failed",
  "next_action": "retry_connect"
}
```

and calls:

```text
defer(WorkLease, checkpoint_json, not_before = now + backoff)
```

This moves `X` to `DEFERRED` without incrementing Singular recovery attempts. A later `Resume` reads the checkpoint and task policy decides whether to retry, increase backoff, defer again, or terminally `fail()` if the task retry budget/deadline is exhausted.

## Singular API Changes
Split the public claim path by intent: `start()` for brand-new work and `resume()` for known existing work. This keeps the fresh path insert-only and the existing path state-transition-only.

Current ambiguity to remove:

- Same `lease_owner` claims a `WORKING` item.
- Caller receives a result that can be interpreted as `Claimed`.
- Bookkeeper may spawn another worker for the same logical work.

Proposed outcomes:

| Outcome | Meaning | Bookkeeper action |
|---|---|---|
| `Granted(Fresh, WorkLease)` | New item moved to `WORKING`; Singular accepted the proposed lease token and initialized recovery attempt count. | Spawn worker with `WorkLease`. |
| `Granted(Resume, WorkLease)` | Due `DEFERRED` item moved to `WORKING`; Singular accepted the proposed lease token without incrementing recovery attempt count. | Spawn/resume worker with `WorkLease`. |
| `Granted(Reclaim, WorkLease)` | Expired `WORKING` item moved to this owner; Singular accepted the proposed lease token and incremented recovery attempt count. | Spawn recovery worker with `WorkLease`; resume from checkpoint/progress for `X`. |
| `Active(lease_expires_at)` | Item is `WORKING` under a valid lease (no owner — descriptive only). | Return `IN_PROGRESS`; do not spawn. |
| `Deferred(DeferredInfo)` | Item is parked until `not_before` and has no active owner. | Return `IN_PROGRESS`/waiting metadata including `not_before`; do not spawn. |
| `Terminal(TerminalResult)` | Item is terminal: `DONE`, `FAILED`, or `INDETERMINATE`. | Return/replay terminal result; do not spawn. |
| `NotFound` | `resume()` was called for missing `X`. | Treat as protocol error for now. |

The exact enum names can change during implementation, but the behavioral split is required: only `Granted` carries a `WorkLease`.

## Lease Renewal
Long-running workers must renew their lease during work that can exceed the lease timeout.

Worker rule (the renewal API is `extend_lease()`):

1. Start a periodic `extend_lease()` loop before side-effect phases.
2. If `extend_lease()` returns `Extended`, continue.
3. If it returns `TokenStale`, `NotFound`, or `Terminal(result)`, stop before performing more side effects.
4. Log the stale-worker exit with task key, lease owner, and the outcome.

Renewal does not replace external idempotency. It limits how long a stale worker can keep acting after it loses ownership.

## Terminal Callback Fencing — Actionable-State Contract
Singular's terminal mutators expose **actionable state**, not request history. Every outcome tells
the caller what to do with the Microflows callback. There is no `Duplicate` (it describes the request,
not the authoritative result), and **no `Applied`/`Terminal` split** — whether this call performed the
transition or the step was already settled, the action is identical. `complete()`/`fail()` return:

- **`Settled(result)`** — the step is terminal. Deliver the **authoritative** `result` (the state +
  payload Singular holds, as a payload-bearing `TerminalResult`) to Microflows on the **current**
  dispatch correlation token; do **not** repeat the business effect. This single outcome covers a
  first write, the writer's own retry after an ambiguous commit response, **and** a cross-terminal
  call (e.g. `complete()` on a FAILED item → `Settled(Failed)`, so the worker delivers the *failure*,
  never a false `FINISHED`). First-write-vs-replay provenance, if ever needed, is a `SingularEvent`,
  not a separate outcome.
- **`TokenStale`** — a superseded/foreign token (you lost the lease). Suppress callbacks and exit;
  you are not the delivery owner. Log + increment a stale-suppression metric.
- **`NotFound`** — protocol/integration failure; alert/recover.

`Settled` vs `TokenStale` on a settled item: the token that wrote the terminal state
(`terminal_lease_token`) gets `Settled` with the authoritative result; any other token is `TokenStale`
(`current_lease_token` is cleared once terminal).

### Terminal replay is active recovery, not a no-op
A caller that observes a terminal result it did not just produce — `resume() → Terminal(result)`, or
`complete()/fail() → Settled(result)` on an already-settled step — becomes the
**delivery/reconciliation owner** for the current dispatch. It MUST:
1. Not perform or repeat the business effect (the effect is already settled).
2. Reconcile if the terminal state requires it (e.g. a later `INDETERMINATE`, or verifying external state).
3. Deliver the stored authoritative outcome to Microflows, echoing the **current** dispatch
   correlation token (not whatever token the original node used).
4. Treat the dispatch as resolved only *after* delivery; if delivery fails, exit without marking
   resolution so MF resubmits and the next node inherits the same delivery duty. Delivery is therefore
   at-least-once and idempotent at the MF layer (correlation-token dedupe); two racing resumers both
   delivering is fine.

This is what closes the "node committed terminal but died before notifying MF" gap: the replacement
node learns the result via `start→Exists→resume→Terminal(result)` (it holds no lease, so it cannot
re-charge) and replays the callback. The crash window *after* Singular commits but *before* delivery
is irreducible from Singular state alone — resubmit + status-poll repairs it; only a durable callback
outbox with retry would make delivery unsolicited-guaranteed, which is out of scope for Singular.

This keeps callbacks aligned with Singular's terminal record, prevents stale workers from telling
Microflows a result Singular did not accept, and makes a settled step's notification an explicit
obligation rather than a silent skip.

## External Idempotency Keys
Every irreversible downstream operation must carry a stable operation-level idempotency key.

For card charging:

- Use a processor idempotency key derived from `X`, the stable sale/payment-attempt charge-step identity.
- Do not derive it from worker id, node id, process id, lease owner, lease attempt, or retry count.
- Treat Singular as the task coordination layer and the payment processor idempotency key as the duplicate-charge guard.

Example key shape for review, not final API:

```text
pushcoin.sale-payment-attempt.<sale_id>.<payment_attempt_id>.charge
```

Other irreversible effects, such as issuing refunds, sending settlement instructions, or creating immutable external records, need equivalent operation-specific keys.

## Bookkeeper Changes
Submit handling:

- On `Granted(Fresh, WorkLease)`, spawn one normal worker.
- On `Granted(Resume|Reclaim, WorkLease)`, spawn one recovery/resume worker that resumes from checkpoint/progress for `X` before retrying irreversible work.
- On `Active`, return `IN_PROGRESS` and do not spawn.
- On `Deferred`, return `IN_PROGRESS` with `not_before` and do not spawn.
- On `Terminal(DONE|FAILED|INDETERMINATE)`, replay the terminal response and surface `INDETERMINATE` distinctly from success/failure.
- On `NotFound` from `resume()`, treat as protocol error for now.

Worker execution:

- Renew leases for synthetic and real tasks that can run longer than the lease timeout.
- Record non-terminal progress and defer when an external system says the operation is accepted but not terminal.
- Stop work when renewal reports loss of ownership or terminal state.
- Gate the callback on the `complete()`/`fail()` `SettleOutcome` under the `WorkLease`: deliver the
  authoritative `result` on `Settled(result)`; suppress on `TokenStale`; alert on `NotFound`.
- Suppress callbacks from stale workers (`TokenStale`).

Logging:

- Log reclaims distinctly from fresh claims.
- Log duplicate/in-progress submits (the `Active` outcome). Ownership is descriptive only and is not
  surfaced on `Active`; the finalizing/holding owner, if needed, is read from `history()`/`inspect`.
- Log recovery attempt number and max-recovery-attempt policy.
- Log stale-worker exits at renewal and terminal-write fences.

## Microflows Contract Documentation
Update Microflows/bookkeeper protocol docs after the design is accepted to state the dispatch/callback guarantee without naming Singular internals:

> Callback delivery is at-least-once and replayable. Each dispatch carries an opaque sequence/correlation token, and callbacks/responses must echo it so Microflows can reject stale or out-of-sequence callbacks.

The protocol docs should also say:

- `IN_PROGRESS` can represent fresh work, active work, or retryable/reclaimed work.
- `GET .../status` remains the fallback read of Singular state.
- Callback delivery is not exactly-once; consumers must tolerate replay.
- The dispatch sequence/correlation token is a Microflows protocol concept, independent of Singular.

## Regression Tests To Pin
Singular:

1. First `start()` for `X` returns `WorkLease` with attempt `1` and lease expiry.
2. Duplicate `start()` for existing `X` does not run resume/reclaim logic and returns no `WorkLease`.
3. `resume()` while `X` is `WORKING` with a valid lease receives `Active` and no `WorkLease`.
4. Different owner after lease expiry reclaims successfully, receives a new lease token, and recovery attempt count increments.
5. Old lease token after reclaim cannot `renew`, `complete`, `fail`, `defer`, or checkpoint.
6. Non-owner or stale-token post-reclaim cannot mutate terminal result.
7. Active duplicate claims do not increment recovery attempt count.
8. Due `DEFERRED` resume grants `WorkLease(Resume)` without incrementing recovery attempt count.
9. Last allowed reclaim returns `WorkLease` with final-recovery-attempt metadata.
10. Any current `WorkLease` can terminally `fail()` when the task result is known unrecoverable.
11. Expired/non-terminal claim after `max_recovery_attempts` have already been granted atomically transitions to `FAILED(attempts_exhausted)`.
12. Current `WorkLease` can terminally mark `INDETERMINATE` when external state is unsafe to retry and unsafe to fail.
13. Accidental same `worker_id` across two gateways does not spawn duplicate work on duplicate submit.

Bookkeeper in-process:

1. Duplicate submit while first worker is active spawns exactly one worker.
2. Same-owner duplicate submit returns `IN_PROGRESS`.
3. Worker that loses its lease token before `complete()` does not send `FINISHED`.
4. Worker that loses its lease token before `fail()` does not send `FAILED`.
5. Long-running synthetic task renews lease and remains owner until completion.
6. Synthetic card-charge-like task records non-terminal progress/defer and resumes from that checkpoint.

HTTP driver:

1. Concurrent duplicate `POST`s for `microflow-proto-check` produce one callback and one terminal record.
2. Forced stale-worker scenario suppresses stale callback.
3. Existing happy/failure/status matrix remains green.

## Review Questions
1. Should renew rotate `lease_token`, or is rotation on claim/reclaim enough for the first hardening pass?
2. Should `max_recovery_attempts` be a claim argument, stored in `item_meta`, or both?
3. What renewal interval should bookkeeper use relative to lease timeout?
4. Which downstream operations in the first sale workflow are irreversible and therefore require explicit idempotency keys?
5. Should callback suppression from stale workers be silent beyond logs, or should it increment a metric/counter?

## Out Of Scope
- Full sale workflow modeling.
- Provider-specific payment integration.
- Temporary workarounds that mask core protocol defects.
- Refactors unrelated to claim, renewal, terminal fencing, callback emission, and idempotency-key documentation.

## Completion Criteria For Implementation Later
This hardening is complete only when both are true:

- Regression tests pin lease-token fencing, stale-worker, duplicate-submit, recovery-attempt-count, defer/resume, and reclaim behaviors.
- Root-cause protocol changes are implemented in Singular and bookkeeper without relying on semantic masking.

---

# PR1 Decision Log (current contract = this document)

`plan.md` is the single current contract. `implementation-plan.md` is the PR sequencing /
implementation tracker; `pr1-sql-api-diff.md` is retired (implemented — see its stub). Earlier
docs' executable-looking sketches that show superseded shapes (`renew()`, `ActiveInfo`,
`Granted(Reclaim)` wording, same-owner logging, `Duplicate`, `Applied`/`Terminal` split) are **not**
the contract — this log is the history.

As-built PR1 public surface (gateway `singular/packages/singular/src/`): `WorkLease{key, lease_token,
lease_expires_at}` · `TerminalResult{Done(response_json)|Failed(error_json)}` · `StartOutcome{Granted|
Exists}` · `ResumeOutcome{Active(lease_expires_at)|Terminal(result)|NotFound}` · `SettleOutcome{Settled
(result)|TokenStale|NotFound}` (complete + fail) · `ExtendLeaseOutcome{Extended|Terminal(result)|
TokenStale|NotFound}` · `WorkEvent{Claimed|Extended|Completed|Failed}` · `InspectOutcome{Working(owner,
expires,checkpoint)|Terminal(result,checkpoint)|NotFound}` · `HistoryEntry` (raw audit, event only).

Decisions (most recent first):
- **Discriminated single-JSON-result + DB object-document contract (2026-06-07):** the positional
  result sets (still carrying nullable `status_code`/token/`lease_expires_at` primitives) were replaced
  — every actionable SP returns ONE `result` column, a JSON OBJECT keyed by `outcome`, arm-inapplicable
  fields OMITTED (no NULL-as-state at the boundary). ALL DB JSON is now a non-NULL JSON OBJECT (inputs +
  persisted columns + result docs): the empty document is `{}` (never SQL NULL / JSON null / top-level
  array / top-level scalar; nested arrays are fine), enforced by schema
  `CHECK(json_valid AND json_type='OBJECT')` + `NOT NULL`, SP `SIGNAL`, AND the gateway on both sides
  (before SQL → `InvalidJson`; on decode → `BackendResponseInvalid`). Gateway decode:
  `_read_result_doc`/`_doc_*`/`_terminal_from_doc`; payload/checkpoint are nested JSON objects
  (re-encoded compact for delivery; the stored record stays immutable); `lease_owner` as
  `LOWER(HEX())` ⇄ `codec.hex_decode`; `start` threads the caller's input token. Public gateway
  variants UNCHANGED. This **supersedes** the prior
  "Unified terminal payload + `SETTLED(1)`" positional shape below and **resolves** the PR2 deviation
  that `inspect` encoded NotFound as an all-NULL row (it is now `{"outcome":"not_found"}`). New
  regressions: e2e scenario 16 (before-SQL contract) + the rebuilt `singular_malformed` decode fixture
  + SP-input object-contract cases. (The SP-internal projection-presence check still uses
  `v_current_event_ts IS NULL`, an internal var test — safe, `current_event_ts` is NOT NULL — left for
  PR2.) **Review follow-ups (same day):** payload/checkpoint embed as NESTED objects (not
  JSON-in-a-string), re-encoded via `json.encode_compact`; `history.lease_owner` is `NOT NULL` and SP
  owner params are `varbinary(16)` validated to exactly 16 bytes (`SingularLeaseOwnerInvalid`); PR1
  `resume` is non-locking (dropped `FOR UPDATE`); and `history()` transport is reshaped to per-row JSON
  documents (`event` discriminator, inapplicable `lease_expires_at` omitted) with `Array<HistoryEntry>`
  unchanged publicly. A later pass restored event/status corruption validation that the history
  reshape had dropped — a schema `CHECK ck_singular_history_event_status` makes a mismatched pair
  unrepresentable, and history transports `status` so the gateway re-cross-checks it on decode
  (`_check_event_status`) then drops it (pinned by an sp-invariant CHECK test + a malformed-history
  fixture); and owner-input regressions (NULL/short → `SingularLeaseOwnerInvalid`) were added.
  See [[feedback_sql_design_preferences]] (object-document rule) and
  [[reference_drift_v1_syntax_quirks]] (constructor-arg-throw leak quirk worked around).
- **Strict readers (CORE_BUG):** required result fields (the discriminant `outcome`, terminal
  `state`/`payload`, required history fields incl. `item_meta`) reject NULL/read-failure/non-object as
  `BackendResponseInvalid`; never a silent default. (The granted token is no longer read from the
  result — `start` threads the caller's validated input token.) **Pinned in the normal gate**
  (`just test`): the isolated `singular_malformed` fixture (loaded by `just db-load-schema`, NOT the
  product schema) + `malformed_backend_test` drive the DECODE-side object contract across `inspect`
  keys — envelope + nested payload + owner-hex/checkpoint, accept + reject (SQL NULL / malformed /
  JSON null / array / scalar).
- **Terminal-payload object contract (both boundaries):** the payload is a non-NULL JSON **object**
  (see the top reshape entry — supersedes the earlier "unified `payload_json` column / valid-JSON"
  wording). SPs `SIGNAL` and the gateway raises caller-input **`InvalidJson(field)`** (NOT
  backend-rejection) on empty / JSON null / array / scalar / malformed; an absent document is `{}`,
  never JSON null. The decode side (`_terminal_from_doc`) requires a nested object and re-encodes it.
- **`resume(key)` only (PR1):** dropped the PR2-only inputs (meta/timeout/max-recovery/token) — a
  malformed proposed token could otherwise reject/block a legitimate terminal replay; structured recovery
  request returns in PR2.
- **Dangling head-history is corruption, not NotFound:** every SP reads the projection first
  (absent → NotFound) and then the referenced head-history row with an explicit presence check
  (`NOT FOUND` handler + flag, not NULL-inference) → `SIGNAL SQLSTATE '45001' ..., MYSQL_ERRNO = 30001
  SingularHeadHistoryMissing`. The gateway classifies `error_code == 30001` (a code < 2^15 so every
  client reads it identically) as `BackendResponseInvalid` (vs `BackendRejected` otherwise). Pinned by
  the raw-SQL/SP-invariant track (`sp_invariants_test.py`) which runs in the cert gate as a serialized
  `DB_GROUP` job via the same executor (per-run nonce service_groups, exact cleanup); `just test-sql`
  is the standalone dev runner. *Known deviation (PR2):* the SP-internal projection-presence check
  still uses `v_current_event_ts IS NULL` — currently safe (`current_event_ts` is NOT NULL), to be made
  an explicit found-flag in PR2. (The `inspect`-NotFound-as-all-NULL-row deviation is RESOLVED by the
  2026-06-07 discriminated-JSON reshape above — it is now `{"outcome":"not_found"}`.) See
  [[feedback_sql_design_preferences]].
- **Unknown backend result code → `BackendResponseInvalid`** (was `BackendRejected`).
- **Actionable-state terminal contract:** no `Duplicate`; `complete`/`fail` → one `SettleOutcome`
  (`Settled(result)` covers first-write, replay, and cross-terminal); deliver `result`, suppress on
  `TokenStale`, alert on `NotFound`. `resume`/`extend_lease` keep `Terminal(result)`.
- **`TerminalResult` payload-bearing variant** (illegal state/payload combos unrepresentable);
  `TerminalState` enum removed. `Indeterminate`/`Failed.reason` added when produced (PR4/PR2).
- **`inspect` outcome union** (`Working`/`Terminal`/`NotFound`); `Inspection` struct, public `WorkState`,
  `ActiveInfo`, `same_owner`, and `HistorySummary` removed. `HistoryEntry` is a documented raw-audit row
  (event only; event↔status validated adapter-privately).
- **Live leases are bounded (no unbounded lease):** an unbounded `WORKING` lease can never be
  reclaimed, so `start`/`extend_lease` require a **positive** `lease_timeout_seconds` — gateway rejects
  client-side as caller-input `InvalidLeaseTimeout`; SPs `SIGNAL SingularLeaseTimeoutInvalid`; and a
  schema CHECK enforces `status=WORKING ⇒ lease_expires_at IS NOT NULL`. Regressions cover zero/negative
  (NULL is not representable through the Int gateway; SP-guarded as defense).
- **`extend_lease`:** never shortens (`GREATEST` over the now-non-null existing expiry); terminal-fenced
  by `terminal_lease_token`.
- **`start`:** explicit `HANDLER FOR 1062` (not `INSERT IGNORE`).
- **Backend boundary:** numeric codes / positional columns / SP names / SIGNALs / diagnostics stay
  inside the MariaDB adapter; callers branch on domain variants only.

(The SQL batch — `SETTLED(1)`, strict JSON inputs, `resume(key)` — was approved and applied
2026-06-06; the result-set SHAPES were then reshaped 2026-06-07 to one discriminated `result` JSON
document per SP under the object-document contract — see the top decision. As-built in
`singular/db/procs/` + `singular/db/schema/`.)

PR2 reintroduces (when producible, as a DIRECT signal — never inferred from checkpoint emptiness):
`WorkMode{Fresh, Recovery}` + recovery-attempt fields, `WorkLease.checkpoint_json`, reclaim-via-resume.
