# Singular PR2 — expired-lease reclaim (effort charter)

## Objective

Make Singular grant a fresh attempt on an **expired** working item so a crashed participant's operation can
be recovered under the **same** `idempotency_key`. Today `resume()` only observes (`Active`/`Terminal`/
`NotFound`) and never rotates the token, so after the dangerous window — *participant commits its side
effect → crashes before `complete()` → lease expires* — the workflow polls 202 forever and never observes
the result. This is the documented-but-unimplemented PR2 extension (`singular/doc/singular-protocol.md` §0.3
marks reclaim **REQUIRED**); it blocks coordinator-compatible exactly-once recovery (PushCoin Phase 7).

## Design (normative: protocol §4 + §6.6)

- **Token rotation fences, not wall-clock (§4).** Wall-clock expiry only makes an item *reclaimable*;
  authority is revoked by **rotating the stored `current_lease_token`**. A reclaim grants a new token to the
  new attempt, so a revived original finds its token stale and can never publish.
- **resume/reclaim (§6.6).** One operation, optional recovery inputs. terminal ⇒ replay; live working ⇒
  `Active` (never steal); **expired working + recovery lease ⇒ `Granted`** (fresh attempt + rotated token,
  handing over the persisted checkpoint).
- **Single SP, dual mode.** `sp_singular_resume` takes nullable recovery params `(lease_owner, lease_meta,
  event_ts, lease_expires_at, lease_token)`. NULL token ⇒ read-only resume (PR1 behaviour preserved). The
  recovery token is validated **only** on the reclaim-eligible (working) path, so a malformed token can
  never block a terminal replay (the reason PR1 dropped these inputs).
- **recovery_attempt** is derived from the CLAIMED-event history (start = #1; first reclaim = 1), returned on
  the grant; the handed-over **checkpoint** is the carried-forward context document.

## Boundaries

- **In scope:** `sp_singular_resume` reclaim; the gateway `resume(key, recovery)` API + `ResumeOutcome::
  Granted`; SQL-level conformance coverage; the e2e + participant-stub reclaim path; the dangerous-window
  end-to-end test.
- **Out of scope:** `defer` / deferred state (§6.7, a separate REQUIRED extension); input-identity conflict
  detection (§1.3); any change to the §4 token-rotation *rule* (it is the agreed contract — implement it,
  don't revise it). No automatic on-expiry state transition (an expired item just becomes reclaimable).
- **Invariant:** SP, gateway, and conformance tests MUST agree on the token-rotation rule (§4). The SP arg
  count is fixed, so the SP and the gateway resume() call MUST land together — there is no SQL-only landable
  step.

See `PROGRESS.md` for current status and the remaining build steps.
