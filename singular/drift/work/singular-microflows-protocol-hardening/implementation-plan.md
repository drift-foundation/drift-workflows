# Singular / Microflows Protocol Hardening — Implementation Plan

**Owner:** K (implementation lead). **Status:** PR1 + the JSON object-document reshape + review rounds
IMPLEMENTED on `main` working tree (SQL + gateway + tests), **green at 16/16 on drift 0.33.26 / abi 16**
(re-anchored from 0.33.24/abi15; author-claim SCI `cf488f3b`, version 0.4.0); PR2–PR7 pending.
**Date:** 2026-06-04, rev 2026-06-08. **Current contract + decision log:** [`plan.md`](plan.md) —
authoritative. This file is the PR sequencing / implementation tracker. **Decision:** breaking Singular
API is acceptable (bookkeeper is the only consumer). SP/schema `.sql` is reviewed before any code (see §9).

> **HAND-OFF (2026-06-08):** Singular + Microflows are being extracted to a dedicated repo and folded
> into the drift toolchain/cert (0.33.37+). The work on `main` here is being wrapped up green + coherent
> for a clean hand-off. **The Determinism reshape below is PARKED — implement it in the new repo**, now
> that microsecond `std.time` is available (`utc_unix_micros` / `utc_from_unix_micros`, 0.33.26+).

## Determinism reshape (PARKED — next major work; implement on µs `std.time`)

**Principle (user, 2026-06-07):** a Singular transition is a pure function of `(existing persisted state,
explicit call arguments)`. Two independent stores given the same initial state + command sequence MUST
produce the same persisted state and outcomes. ⇒ no database clock, no implicit/adjusted protocol time,
no event-ts repair; the caller supplies every timestamp.

**Sweep target (all in the 4 mutators; schema already has no `DEFAULT`/`ON UPDATE`):** drop
`UTC_TIMESTAMP(6)`, `DATE_ADD(now,…)`, and the `+ INTERVAL 1 MICROSECOND` repair from
`sp_singular_start`/`complete`/`fail`/`extend_lease`. `created_at`/`updated_at`/`current_event_ts` and
history `event_ts` all become `arg_event_ts`.

**Locked decisions (with the user):**
- **Timestamps are a typed `std.time.UtcTimestamp`** at the public API (µs precision, available 0.33.26+;
  std.time was ms-only through 0.33.24, which is why this was parked). The **`SingularImpl` MariaDB adapter**
  converts: `UtcTimestamp` → native MariaDB `DATETIME(6)` string for binding (mariadb-rpc exposes only
  `arg_string`/`arg_int`/`arg_bytes`/`arg_null` — no `arg_datetime`), and parses outbound `DATETIME(6)`
  text → `UtcTimestamp`. **No RFC-3339/ISO text reaches SQL**; SPs receive `DATETIME(6)` and do direct
  comparisons only. Canonical neutral form stays out of the SP; conversion lives only in the adapter.
- **`EventTimeConflict(current_event_ts: UtcTimestamp)` is a THROWN `RuntimeErrorKind`**, not an outcome
  arm — it's a violated ordering precondition, not item state (unlike TokenStale/NotFound). No mutation;
  bubble to MF/job orchestration; the gateway must NOT auto-retry. The SP surfaces it as a structured
  `{"outcome":"event_time_conflict","current_event_ts":…}` result doc; the adapter maps that doc to the
  throw (carrying the authoritative watermark). Avoids adding the same exceptional arm to every mutator.
- **Replay-before-timestamp ordering:** resolve NotFound → corruption(dangling head) → **terminal/token
  replay FIRST** (a terminal item returns the stored result / TokenStale with NO time check); only on
  WORKING + matching token apply the append-time check `arg_event_ts > current_event_ts`. TokenStale
  precedes EventTimeConflict (never hand a stale worker a watermark).
- **Idempotent-retry contract:** the caller retains each command's `event_ts`; a retry of the SAME command
  reuses that exact ts; a NEW mutation supplies a strictly-later ts; an older ts throws the conflict. For
  `extend_lease`, treat `arg_event_ts == current_event_ts` **with a matching recorded extension** as an
  idempotent retry (return the recorded result), NOT a conflict — otherwise an uncertain committed
  extension would falsely conflict. Apply the same explicit retry rule to all future non-terminal mutators.
- **Lease expiry supplied explicitly** (not computed from DB time + timeout): replace
  `lease_timeout_seconds: Int` with `lease_expires_at: UtcTimestamp`. **SP-authoritative** validation
  `lease_expires_at > arg_event_ts` → `SingularLeaseExpiryInvalid` (distinct errno, e.g. 30002, mapped by
  the gateway to a thrown `InvalidLeaseExpiry`); the gateway does NOT pre-compare strings (no format
  assumptions). Extension never shortens: `lease_expires_at = GREATEST(existing, arg_lease_expires_at)`.
- **Nothing added to `WorkLease`** yet — the caller already supplied the granted ts and retains its own
  per-command ts; `EventTimeConflict.current_event_ts` is the correction watermark. Revisit only if PR2
  recovery grants produce an authoritative ts the caller did not supply.
- **Future (PR2–PR4), same rule:** `not_before` (defer), reclaim / expiry-vs-now evaluation time, and
  INDETERMINATE all take caller-supplied evaluation timestamps — the command supplies the relevant time.

**Proposed signatures** (`import std.time as time`):
```
fn start(self, key, item_meta, lease_meta, event_ts: time.UtcTimestamp, lease_expires_at: time.UtcTimestamp, lease_token) -> StartOutcome
fn resume(self, key) -> ResumeOutcome                                              // unchanged (read-only)
fn complete(self, lease, event_ts: time.UtcTimestamp, response_json) -> SettleOutcome
fn fail(self, lease, event_ts: time.UtcTimestamp, error_json) -> SettleOutcome
fn extend_lease(self, lease, event_ts: time.UtcTimestamp, lease_expires_at: time.UtcTimestamp) -> ExtendLeaseOutcome
fn inspect(self, key) -> InspectOutcome                                            // logic unchanged
fn history(self, key) -> Array<HistoryEntry>                                       // logic unchanged
```
- **Outbound timestamps become `UtcTimestamp` too** (adapter parses `DATETIME(6)` → `UtcTimestamp`):
  `WorkLease.lease_expires_at`, `ResumeOutcome::Active`, `InspectOutcome::Working.lease_expires_at`,
  `HistoryEntry.event_ts`, `EventTimeConflict.current_event_ts`. `HistoryEntry.lease_expires_at` is
  absent for non-lease events → model as `Optional<time.UtcTimestamp>`.
- **Error taxonomy:** replace `RuntimeErrorKind::InvalidLeaseTimeout(seconds: Int)` →
  `InvalidLeaseExpiry`; add `EventTimeConflict(current_event_ts: time.UtcTimestamp)`. Drop `_validate_timeout`.
- **Schema:** no structural time change required (columns already plain `DATETIME(6)`); SPs change to
  `arg_event_ts`. Optional hardening: `CHECK (updated_at >= created_at)`, history
  `CHECK (lease_expires_at IS NULL OR lease_expires_at > event_ts)`.
- **Test ripple:** e2e (~46 mutator call sites + `_must_start`) and `sp_invariants_test.py` (15 SP calls)
  thread a **deterministic monotonic `UtcTimestamp` generator** + explicit expiries; timeout tests become
  expiry tests; add scenarios for `EventTimeConflict` (stale ts after replay still returns the terminal
  result), `InvalidLeaseExpiry`, and the `extend_lease` idempotent same-ts retry.

**Open sub-decision deferred at park time (Q3 of the ripple report):** confirmed SP-authoritative expiry
validation (no gateway pre-compare). All other Q's resolved above.

## Revisions (K review, 2026-06-04) — authoritative
1. **Lease tokens are APP-minted.** The **caller (app/bookkeeper)** generates a 16-byte CSPRNG
   token and *proposes* it to **`start()`** (PR1's only grant path; token-carrying `resume()` is
   deferred to PR2 — PR1 is `resume(key)`); the gateway **accepts a caller-provided token and never
   silently mints internally** (a `new_lease_token()` helper is fine, but callers/tests must be able
   to supply deterministic tokens). Singular validates/fences/stores it only when it
   grants ownership, and never generates tokens itself. (Reproducible tests, no MariaDB RNG
   dependency, DB portability.)
2. **Terminal token memory.** Terminal writes do **not** wipe all token authority. A
   `terminal_lease_token` column records the token that wrote the terminal state, so a same-token
   retry → the authoritative result (`complete()`/`fail()` → `Settled(result)`; `resume()`/
   `extend_lease()` → `Terminal(result)`) while a stale old token → `TokenStale`.
   (Rev v3 #11 / v4 #17 replaced the original `Duplicate` outcome with the actionable-state result.)
3. **Claim outcomes (as-built PR1; updated by Rev v4):** `start` → `Granted(WorkLease) | Exists`;
   `resume` → `Active(lease_expires_at) | Terminal(TerminalResult) | NotFound` (PR2 adds
   `Granted`/`Deferred`). `TerminalResult` is a **payload-bearing variant** `Done(response_json) |
   Failed(error_json)` (`Indeterminate`/`reason` added when produced). No `ActiveInfo`,
   `AlreadyDone`, or `AttemptsExhausted`; exhaustion (PR2) = `Terminal(Failed, …)`. complete/fail
   share one `SettleOutcome` (Rev v4 #17).
4. **`fail()` is terminal-only** — drop `retryable`. `fail(lease, error)` → terminal FAILED.
   Non-terminal retry/backoff/preemption uses `defer(lease, checkpoint, not_before)`; immediate
   retry = `defer` with `not_before <= now`. No worker API returns a "READY" state.
5. **No mixed-version compatibility.** Clean break — no 0.4 behavior preserved, no
   `sp_singular_reclaim` shim. Bookkeeper/root gates may be **temporarily skipped (with reasons)**
   during the coordinated Singular→bookkeeper migration, then restored.
6. **Phasing stays** for SQL-review/regression discipline, but correctness > intermediate
   stability — do not contort for 0.4/0.5 coexistence.

## Revisions v2 (final, 2026-06-04)
7. **`READY` dropped** (confirmed). State set = **`WORKING / DEFERRED / DONE / FAILED /
   INDETERMINATE`**. "Claimable" = *no row yet* → `start`; or `DEFERRED` with `not_before <= now`
   → `resume`. (Design doc `plan.md` synced by the owner.)
8. **`claim` split into `start` + `resume`** — distinct API/SP intent, no mega-claim:
   - `start(key, item_meta, lease_meta, lease_timeout, lease_token)` — **brand-new work only**.
     The PK `INSERT` into `tb_singular_work_item` IS the serializer: one caller wins →
     `Granted(Fresh)` (row created, `WORKING`); a PK/unique conflict → `Exists` (caller then
     `resume`s). **No select-then-insert.**
   - `resume(...)` — **existing work only; never inserts**. (PR1 as-built is **`resume(key)`** only —
     it grants nothing, so it takes no token/meta/timeout/max-recovery; Rev v4 #21 / §3. The full
     signature `resume(key, lease_meta, lease_timeout, max_recovery_attempts, lease_token)` returns in
     PR2 when it can grant.) Handles active `WORKING` (`Active`), expired-`WORKING` reclaim
     (`Granted(Reclaim)`), due/not-due `DEFERRED` (`Granted(Resume)`/`Deferred`), terminal
     (`Terminal`), missing row (`NotFound`) — reclaim/deferred land in PR2/PR3.
9. **Explicit token validation** on `start` in PR1 (token-carrying `resume()` deferred to PR2 — PR1
   is `resume(key)`, no token to validate): token **required** (NOT NULL), **exactly 16 bytes**,
   malformed/null → rejected (protocol error); the proposed token is **stored only on grant**,
   ignored on any non-grant outcome; **never returned by
   `inspect`**.
10. **Storage stays projection + journal** this pass: `tb_singular_work_item` = current-state
    projection, `tb_singular_work_item_history` = append journal. Every transition **appends**
    history and updates the projection; **avoid select-then-insert**. Designed toward a future
    append-only/state-table queue without changing the protocol.

## Revisions v3 (reviewer findings, 2026-06-06) — authoritative
11. **Actionable-state terminal contract (no `Duplicate`).** *(The `Applied`/`Terminal` split below
    was SUPERSEDED by Rev v4 #17 — `complete`/`fail` now return a single `SettleOutcome { Settled(result)
    | TokenStale | NotFound }`. The contract is otherwise unchanged: `Settled` = deliver the authoritative
    result, regardless of first-write vs replay.)* Historical: complete/fail exposed
    `Applied(TerminalResult) | Terminal(TerminalResult) | TokenStale | NotFound`. `Applied` =
    this call did the transition (deliver). `Terminal(existing)` = already settled (the writer's
    own token); it carries the **authoritative** state+payload and the caller **redelivers** it —
    terminal replay is **active delivery/reconciliation work**, not a no-op. The node that observes a
    terminal result it did not produce is the delivery owner: do not repeat the effect, deliver the
    stored outcome on the **current** MF correlation token. `TokenStale` = superseded/foreign token →
    suppress (not the delivery owner). True process death → recovery is `start→Exists→resume→Terminal`;
    `complete()`/`fail()` returning a settled result is the narrower same-worker repair after an
    ambiguous commit response.
12. **Cross-terminal fence (finding #1).** On a settled item the writer's own token gets
    `Terminal(actual state)` — so `complete()` on a FAILED item returns `Terminal(Failed)`, never a
    false success; `fail()` on a DONE item returns `Terminal(Done)`. Decision: `arg_lease_token ==
    terminal_lease_token` → authoritative `Terminal`; any other token → `TokenStale`.
13. **`extend_lease` never shortens (finding #2):** new expiry = `GREATEST(existing, now+timeout)`;
    NULL (unbounded) preserved.
14. **`start` dup-detection is explicit (finding #3):** plain INSERT + scoped `HANDLER FOR 1062`,
    not `INSERT IGNORE` (which would mask truncation/conversion as `Exists`).
15. **Backend boundary:** public gateway outcomes/types are domain-level; SP names, numeric result
    codes, positional columns, SIGNALs, MariaDB diagnostics stay private to `SingularImpl`, which maps
    SQL rows into one logical `TerminalResult`. No caller branches on SQL codes. Behavioral
    (gateway) tests stay separate from any raw-SQL/SP atomicity tests.

## Revisions v4 (actionable-state model trim, 2026-06-06) — authoritative
The public result/data surface was tightened from ~19 to 9 types after a stricter actionable-state
review (semantics first, not type count). Net (the §3 block is the source of truth):
16. **`TerminalResult` is a payload-bearing variant** `{ Done(response_json) | Failed(error_json) }` —
    illegal state/payload combinations are unrepresentable; `TerminalState` is deleted (absorbed).
    `reason` (PR2 "attempts_exhausted") and `Indeterminate(reason, context)` (PR4) are added only when
    produced.
17. **`complete`/`fail` share one `SettleOutcome { Settled(result) | TokenStale | NotFound }`** — no
    `Applied`/`Terminal` split (same caller action: deliver `result`; first-write-vs-replay provenance,
    if needed, goes on `SingularEvent`). The single-field result wrappers
    (`CompleteResult`/`FailResult`/`ExtendLeaseResult`) are removed; methods return the outcome directly.
18. **`inspect` → `InspectOutcome { Working(owner, expires, checkpoint) | Terminal(result, checkpoint) |
    NotFound }`** — no `found` flag, no fabricated state, no lease fields on terminal (the finalizing
    owner is read from `history()`); the terminal checkpoint is kept alongside `TerminalResult` (not
    inside it). `Inspection` struct removed.
19. **`HistoryEntry` exposes `event` only** (no `state`; derivable, and the `(event_type, status)`
    pair is validated adapter-privately → `BackendResponseInvalid` on mismatch). It is a documented
    **raw audit record** — the one type deliberately exempt from the payload-variant rule, as `history()`
    has no control-flow consumer. Public `WorkState` and `HistorySummary` are removed (summary derived
    from `history()` by callers/tests).
20. **`ResumeOutcome::Active` carries only `lease_expires_at`** (`same_owner`/`ActiveInfo` removed —
    descriptive, no caller decision).
21. **`WorkLease` trimmed** to `{ key, lease_token, lease_expires_at }`. `WorkKind`, the
    recovery-attempt fields, and `checkpoint_json` are **deferred to PR2** (only a reclaim/resume grant
    produces a checkpoint; PR1 grants only via `start`). PR2 adds a DIRECT `WorkMode { Fresh, Recovery }`
    signal — fresh-vs-recovery MUST NOT be inferred from `checkpoint_json` emptiness (a recovery lease
    can have an empty checkpoint after a crash before the first checkpoint write).

## Guiding sequencing
1. **Pin Singular behavior first** — schema + SPs + gateway API + Singular's own tests green.
   Bookkeeper stays on the deployed 0.4.0 dist (green) during PR1–4; the cutover happens in PR5.
   Per Rev #5, bookkeeper/HTTP gates may be **temporarily skipped (with reasons)** while PR5 wires
   the new API through, then restored — no 0.4/0.5 coexistence shims.
2. **Regression-first** within each step: write the failing SP/gateway test, then implement to green.
3. **SQL gate:** every SP/schema change is posted as a concrete diff for review (§9) before I write it.

---

## 1. Singular data model (schema deltas)
Current: `tb_singular_work_item` (current-state pointer: `current_event_ts`, `checkpoint_payload`)
+ append-only `tb_singular_work_item_history` (per-event `status`, `lease_owner`, `lease_meta`,
`lease_expires_at`, `event_type`, `event_payload`, `checkpoint_payload`). Status today: `0
PENDING / 1 WORKING / 2 DONE / 3 FAILED`. Mutators fence on `lease_owner <=> arg_lease_owner`.

Deltas (singular is dev-only / `just db-load-schema` rebuilds fresh — **no live migration**, but
recorded for the eventual prod backend):

**`db/schema/tb_singular_work_item.sql`** — add:
- `current_lease_token varbinary(16) NULL` — current capability token (NULL when no active lease; cleared on terminal/defer).
- `terminal_lease_token varbinary(16) NULL` — token that wrote the terminal state (Rev #2); on a terminal item the writer's token gets the authoritative result (`complete`/`fail`→`Settled`, `resume`/`extend_lease`→`Terminal`), any other token → `TokenStale` (Rev v4 #17).
- `recovery_attempt_count int unsigned NOT NULL DEFAULT 0` — fresh + expired-WORKING reclaims only.
- `not_before datetime(6) NULL` — set for DEFERRED.

**`db/schema/tb_singular_work_item_history.sql`** — add:
- `lease_token varbinary(16) NULL` — token granted at this event (audit / trace).

**Status enum (no DDL — value + constant changes), per Rev v2 #7 (`READY` dropped):**
`1 WORKING`, `2 DONE`, `3 FAILED`, `4 DEFERRED`, `5 INDETERMINATE`. (`0 PENDING`/`READY` retired;
"claimable now" = *no row yet* → `start`, or `DEFERRED` with `not_before <= now` → `resume`.)
New event types: extend `CONST_EVENT_*` (e.g. `RESUMED=13`, `DEFERRED=50`, `INDETERMINATE=60`)
alongside existing `CLAIMED=10/EXTENDED=11/RECLAIMED=12/COMPLETED=20/FAILED=40` (event `11` renamed
`RENEWED`→`EXTENDED`). (`RELEASED=30` retired — releases go through `defer`.)

Token minting + validation: **app-minted** (Rev #1). The **caller (app/bookkeeper)** generates a
16-byte CSPRNG token — `std.random.random_secure_bytes(16)`, optionally wrapped as
`new_lease_token()`; tests may supply deterministic tokens — and passes it into
`start()`/`resume()`. The gateway never silently mints. Singular **validates it before any grant**
(Rev v2 #9): NOT NULL and `LENGTH = 16`, else protocol error; stores it as `current_lease_token`
**only on grant**, ignores it on any non-grant outcome, fences on it thereafter, and **never**
returns it via `inspect`. Singular does not call `RANDOM_BYTES`.

---

## 2. Singular SP changes (the core; all `.sql` → review §9)
Result-code taxonomy is unified so the gateway can map cleanly. Proposed per-SP:

The unified claim is **split into `start` + `resume`** (Rev v2 #8), and `sp_singular_reclaim` is
**removed outright** (no shim, Rev #5; reclaim folds into `resume`). Both validate the proposed
token (NOT NULL + 16 bytes, Rev v2 #9) before any grant; both store it as `current_lease_token`
**only** on a grant. Neither does select-then-insert (Rev v2 #10).

**`sp_singular_start`** `(service_group, key, arg_item_meta, arg_lease_meta, arg_lease_timeout,
arg_lease_token)` — **brand-new work only.** The body is a single guarded `INSERT` into
`tb_singular_work_item` (PK `(service_group, key)`) + the matching history append; the PK **is**
the serializer. No prior `SELECT`.
| Outcome of INSERT | Gateway outcome | Token | recovery_attempt |
|---|---|---|---|
| row created (winner) | `Granted(Fresh)` (`WORKING`) | store proposed | set to 1 |
| PK/unique conflict (row already exists) | `Exists` (caller then `resume`s) | ignore | — |

`start` returns no state detail on `Exists` — it never reads the existing row (that's `resume`'s
job). Concurrent `start`s for the same key → exactly one INSERT wins (`Granted(Fresh)`), every
other gets `Exists`.

**`sp_singular_resume`** `(service_group, key, arg_lease_meta, arg_lease_timeout,
arg_max_recovery_attempts, arg_lease_token)` — **existing work only; never inserts.** `SELECT … FOR
UPDATE` the projection row, then a projection update + history append per the matched state. A
missing row is a protocol violation (caller should have `start`ed):
| Current state | Condition | Gateway outcome | Token | recovery_attempt |
|---|---|---|---|---|
| no row | — | `NotFound` (protocol error) | ignore | — |
| `WORKING` | unexpired | `Active(lease_expires_at)` (no owner — descriptive only) | ignore | — |
| `WORKING` | expired, `count < max` | `Granted(Reclaim)` + checkpoint | store proposed | `+1` |
| `WORKING` | expired, `count >= max` | atomically → terminal `FAILED`; `Terminal(FAILED, "attempts_exhausted")` | ignore | — |
| `DEFERRED` | `now >= not_before` | `Granted(Resume)` + checkpoint | store proposed | unchanged |
| `DEFERRED` | `now < not_before` | `Deferred(not_before)` | ignore | — |
| `DONE`/`FAILED`/`INDETERMINATE` | — | `Terminal(state, reason, payload)` | ignore | — |
Grant rows (from either SP) return `WorkLease`: `lease_token` (= accepted proposal), `kind`
(`Fresh` from `start`; `Resume`/`Reclaim` from `resume`), `recovery_attempt`,
`is_final_recovery_attempt` (= `count == max`), `lease_expires_at`, `checkpoint_json` (`""` for
`Fresh`).

**`sp_singular_complete`** — `arg_lease_token` fence (drops `arg_lease_owner` authority). If
not yet terminal and `arg_lease_token <=> current_lease_token` → set `DONE`, set
`terminal_lease_token = arg_lease_token`, **clear `current_lease_token`**. If already terminal:
`arg_lease_token <=> terminal_lease_token` → **`TERMINAL`** carrying the authoritative state+payload
(Rev v3 #11/#12 — supersedes the old `DUPLICATE`; covers same-token replay *and* cross-terminal
complete-on-FAILED), else `TOKEN_STALE`. Otherwise `TOKEN_STALE`/`NOT_FOUND`. On the `WORKING`→`DONE`
write the outcome is `APPLIED(Done, response)`. (`lease_owner` stays descriptive only.)

**`sp_singular_fail`** — **terminal-only (Rev #4): drop `arg_retryable`.** `arg_lease_token`
fence → set terminal `FAILED`, `terminal_lease_token = arg_lease_token`, clear
`current_lease_token`. Terminal-replay + stale handling identical to `complete`. Result:
`APPLIED`/`TERMINAL`/`TOKEN_STALE`/`NOT_FOUND` (Rev v3 #11 — no `DUPLICATE`; the writer's token gets
the authoritative `TERMINAL`). (Non-terminal retry/backoff now goes through `defer`, not `fail`.)

**`sp_singular_extend_lease`** (renamed from `renew` — it only extends the live lease's expiry, no
token rotation, no checkpoint mutation) — `arg_lease_token` fence; extend `lease_expires_at` only.
Result: `EXTENDED`/`TOKEN_STALE`/`NOT_FOUND`/`TERMINAL`.

**NEW `sp_singular_defer`** `(service_group, key, arg_lease_token, checkpoint_json, not_before)` —
token fence; store checkpoint; set `DEFERRED` + `not_before`; **clear `current_lease_token`** (clean
release). Result: `DEFERRED`/`TOKEN_STALE`/`NOT_FOUND`/`TERMINAL`.

**NEW `sp_singular_indeterminate`** `(service_group, key, arg_lease_token, context_json)` — token
fence; set terminal **`INDETERMINATE`** with context; immutable (no resolution path — remediation
is a separate ops job in reference to X). Result: `APPLIED`/`TOKEN_STALE`/`NOT_FOUND`/`TERMINAL`
(actionable-state, Rev v3 #11).

**`sp_singular_inspect`** — return `state` (new enum), `recovery_attempt_count`,
`lease_expires_at`, `not_before`, `response_json`/`error_json`/`checkpoint_json`. **Never return
`lease_token`** (capability, not descriptive).

**`sp_singular_checkpoint`** (if we want progress without defer) — optional; `arg_lease_token`
fence, update checkpoint, keep `WORKING`. Could fold into a future `extend_lease(checkpoint)` overload.

---

## 3. Drift gateway API (Singular 0.5.0)
`singular/packages/singular/src/gateway.drift`. Capability-style: the **caller (app/bookkeeper)
mints** a 16-byte CSPRNG token (`std.random.random_secure_bytes(16)`, optionally via a
`new_lease_token()` helper; deterministic in tests) and proposes it to **`start`** — PR1's only grant
path. **`resume(key)` takes no token** (it grants nothing in PR1; PR2 reintroduces a recovery request
that carries one). The gateway never mints internally; Singular validates (NOT NULL + 16 bytes) and
stores it only on grant. Mutators carry the returned `WorkLease`. No `reclaim()` (folded into `resume`).
`SingularIdentity{service_group, lease_owner}` stays descriptive — audit only, **never an authority
check**; authority is the per-grant token.

> **The PR1 as-built gateway surface is the trimmed model in [`plan.md`](plan.md) (contract + PR1
> Decision Log).** The earlier full sketch that lived here — `WorkKind`, `TerminalState`, `ActiveInfo`,
> `DeferredInfo`, the `CompleteResult`/`FailResult`/`ExtendLeaseResult` wrappers, and the old
> `complete(...)->CompleteResult`/`resume(...,lease_token)` signatures — has been **removed** (it read
> as an executable competing contract). PR2–PR4 reintroduce `WorkMode`, `Deferred`, and
> `Indeterminate` as a forward trajectory, tracked in the Decision Log.

**Submit path (caller):** when the caller **doesn't know** if this is first dispatch, `start` first;
on `Granted(Fresh)` spawn; on `Exists` call `resume` and handle its outcome (one round-trip for a
brand-new key, two for a resubmit). When the orchestration context **already knows** this is a
retry/resume, it may call `resume` **directly** and skip the `start` insert-conflict round trip.
The same-token authoritative-replay outcome (`resume()` → `Terminal(existing)`; `complete()`/`fail()`
→ `Settled(result)`) vs `TokenStale` (old token after reclaim) is decided by the stored
`terminal_lease_token` (Rev #2 / v4 #17, §1/§2). Manifest →
0.5.0; re-cert/redeploy dist; bookkeeper migrates after.

---

## 4. Regression-first test sequence (Singular, before any bookkeeper change)
Gateway-level drift e2e (extend `singular/packages/singular/tests/e2e/live_gateway_test.drift`) +
focused raw-SQL SP tests for the atomic/concurrency cases. Order (red→green):
1. fresh `start` (proposed token T1) → `Granted(Fresh)`, attempt 1, accepted token = T1, `lease_expires_at` set. Concurrent `start`s for the same key → exactly one `Granted(Fresh)`, the rest `Exists`. **(pins start serializer)**
2. `start` on an existing key → `Exists`; `resume` while active WORKING → `Active`, no lease (same and different identity both → Active; merged ex-#3/#4). **(pins same-owner dup)**
3. *(merged into #2 — `Active` is owner-independent)*
4. expired → `resume` with proposed T2 → `Granted(Reclaim)`, accepted token T2, attempt 2, checkpoint returned.
5. stale T1 after reclaim cannot `extend_lease`/`complete`/`fail`/`defer` → `TokenStale`. **(pins fencing)**
6. complete under current T2 → `Settled(Done)`; **same-token retry** of complete(T2) on the now-terminal item → `Settled(Done)` carrying the authoritative payload; **stale** complete(T1) on the terminal item → `TokenStale` (Rev #2/v4 #17, via `terminal_lease_token`).
7. active duplicate `resume`s do not increment recovery count.
8. last reclaim → `is_final_recovery_attempt = true`.
9. any current lease `fail(error)` → terminal FAILED (terminal-only, Rev #4).
10. expired `resume` past `max_recovery_attempts` → atomic terminal FAILED; `resume` returns `Terminal(FAILED, reason="attempts_exhausted")`.
11. `defer(T, checkpoint, not_before)` → `DEFERRED`; `resume` before `not_before` → `Deferred`;
    at/after → `Granted(Resume)` with checkpoint, recovery count unchanged. Immediate retry = `not_before <= now`.
12. `indeterminate(T, ctx)` → terminal INDETERMINATE; `resume` → `Terminal(INDETERMINATE, …)`.
13. accidental same `lease_owner` across two gateways does not let the second mutate (token differs).
14. token validation (PR1): `start`/`complete`/`fail`/`extend_lease` with NULL or non-16-byte token →
    protocol error, no row created/mutated. (`resume(key)` carries no token in PR1 — token-carrying
    `resume()` validation lands in PR2.)
15. missing key: `complete`/`fail`/`extend_lease` on a non-existent `(service_group, key)` → `NotFound` as a **returned result code**, not a SIGNAL. **(pins NotFound-is-data)**
16. cross-terminal fence: `start→fail(T)→complete(T)` → `Settled(Failed)` (never a false success); `start→complete(T)→fail(T)` → `Settled(Done)`; recorded state never flips. **(pins finding #1)**

**Malformed-backend regression (PINNED, 2026-06-07):** an unknown/out-of-range or inconsistent backend
result must surface as `BackendResponseInvalid`, never coerced to a silent `Failed`. Under the
discriminated-JSON-document contract this is: an unknown terminal `state` (the gateway's
`_terminal_from_doc` throws `terminal-unknown-state`), a non-object/missing required field
(`_read_result_doc`/`_doc_*`), a bad/short owner hex (`_hex16`), or a mismatched history
`(event, status)` pair (`_check_event_status`). This can't be driven black-box (the mariadb-rpc client
is SP-only, no product SP emits a bad document, and the mappers are private — exposing them would
breach the §3 backend boundary), so it is pinned on the **isolated `singular_malformed` fixture**
(a separate schema whose `inspect`/`history` SPs return hand-built `result` documents keyed by
idempotency key — accept controls + reject cases across envelope / nested payload / owner-hex /
checkpoint / event-status) plus the **raw-SQL/SP-invariant track** (`sp_invariants_test.py`: the
event/status schema CHECK, the lease_owner input contract, the JSON object-contract on SP inputs, and
the dangling-head corruption errno). Both run in the normal cert gate (`just test`) — the malformed
fixture as an e2e job, the SP track as a serialized `DB_GROUP` job via `emit_test_plan`. The
status=99 sketch is superseded: an invalid terminal is now an unknown `state`/`outcome` or a
mismatched event/status, all covered above.

---

## 5. Bookkeeper adaptation (after Singular 0.5.0 is pinned)
Thread `WorkLease` through `dispatch_task` → handler → worker → mutators.
- **Submit (`start` then `resume`-on-`Exists`, Rev v2 #8):** call `start`; `Granted(Fresh)`→spawn;
  `Exists`→`resume` and match its outcome: `Granted(Resume|Reclaim)`→spawn recovery worker (resume
  from `checkpoint_json`); `Active`→`IN_PROGRESS`, no spawn; `Deferred`→`IN_PROGRESS` + surface
  `not_before`; `NotFound`→protocol error (shouldn't happen after `Exists` — log + 500);
  `Terminal{state,reason,payload}`→replay:
  `DONE`→FINISHED callback, `FAILED`→failure callback (incl. `reason="attempts_exhausted"`),
  `INDETERMINATE`→**distinct alarm** callback/status. Terminal callbacks fire only on the accepting outcome.
- **Worker:** lease-extension loop (`extend_lease` ~⅓ lease timeout) before side-effect phases; stop on
  `TokenStale`/terminal; `defer()` on processor-pending; terminal `complete()/fail()/indeterminate()`
  under the lease, **check outcome before callback**, suppress stale-worker callbacks + emit a
  stale-suppression metric.
- **HTTP mapping:** add `DEFERRED`→`202`(+`not_before`), `INDETERMINATE`→a distinct non-2xx alarm
  code (terminal, do-not-retry, human-needed — *not* the FAILED 422). Echo the MF
  dispatch/correlation token in callbacks/responses.
- Files: `bookkeeper/src/handlers.drift`, `handlers/customers_snapshot.drift`,
  `handlers/microflow_proto_check.drift`, `workers.drift`, `routes/submit.drift`, `routes/status.drift`.

---

## 6. microflow-proto-check coverage (the protocol's test vehicle)
Add a **simulated processor** to the proto-check worker: an injectable, idempotency-key-keyed
store with responses `completed | failed | pending(not_before) | unknown | connect_fail`. New
request toggles (first-class, like the existing ones): `processor_response`, `processor_pending_ms`,
`force_lease_loss` (simulate a reclaim out from under the worker), `connect_fail_n`. HTTP-driver
scenarios (`tests/http/`):
- **duplicate submit:** concurrent `POST`s → exactly one worker, one terminal record, one callback.
- **stale token:** `force_lease_loss` → reclaim by a second attempt → original worker's
  `complete()` is `TokenStale` → no `FINISHED` callback.
- **defer/resume:** `processor_response=pending` → worker `defer()`s with checkpoint, exits;
  resume after `not_before` reads checkpoint, polls, finishes.
- **recovery budget:** force N lease expiries past `max_recovery_attempts` → terminal
  `FAILED(attempts_exhausted)` + fenced callback; recovery count != defer/resume count.
- **indeterminate:** `processor_response=unknown` → terminal INDETERMINATE + alarm callback/status,
  no re-charge.
Plus: existing happy/failure/status matrix stays green; in-process `tests/e2e/*.drift` mirror the
duplicate-submit + stale-token + defer/resume cases.

---

## 7. Protocol docs
`microflows/doc/README.md`: the at-least-once-attempts / effectively-once-effects guarantee; the
`WORKING/DEFERRED/DONE/FAILED/INDETERMINATE` model (no `READY`); terminal kinds incl. INDETERMINATE→ops
remediation; `Deferred`+`not_before`; MF correlation-token callback dedupe (Singular-agnostic);
X-derived external idempotency keys.

---

## 8. PR sequencing (Singular pinned, then bookkeeper)
- **PR1 (minimal first PR):** `start`/`resume` split + app-minted lease tokens + `Active` split +
  stale-token fencing (incl. `terminal_lease_token`). Schema: `current_lease_token` +
  `terminal_lease_token` (+ history `lease_token`). SPs: `sp_singular_start` (PK-insert serializer →
  `Granted(Fresh)`/`Exists`), `sp_singular_resume` (existing-work: **any** `WORKING`→`Active`,
  terminal→`Terminal`, missing→`NotFound`; expired-lease reclaim deferred to PR2, `DEFERRED`/
  `INDETERMINATE` to PR3/PR4), `complete`/`fail` token-fenced → `Settled(result)` (and
  `extend_lease` → `Terminal(result)`) vs `TokenStale` (Rev v4 #17). Gateway: `start()` + `resume(key)`,
  token validation on `start`/mutators only (PR1 `resume` is tokenless), mutators take `WorkLease`. Tests:
  Singular §4 #1,2,3,5,6,13,14 (§4 #14 covers `start`/mutators only — token-carrying `resume` validation
  is PR2). **Pins start serializer + same-owner-dup + stale-token + terminal replay-vs-stale +
  cross-terminal fence.**
- **PR2:** `recovery_attempt_count` + reclaim-via-`resume` + `is_final` + exhaustion→atomic FAILED
  (§4 #4,7,8,10).
- **PR3:** `DEFERRED` + `defer`/`Granted(Resume)` + `not_before` (§4 #11; `READY` dropped, Rev v2 #7).
- **PR4:** `INDETERMINATE` terminal (§4 #12). → **cut Singular 0.5.0** (API now stable).
- **PR5:** bookkeeper migration to 0.5.0 (§5) — relock against the new dist.
- **PR6:** proto-check simulated processor + HTTP/e2e scenarios (§6).
- **PR7:** protocol docs (§7).
Singular SP-level tests gate PR1–4; bookkeeper + HTTP gate PR5–6.

---

## 9. SQL / SP diffs to post for review BEFORE coding (the gate)
Per our rule, I will submit these as concrete `.sql` diffs for your sign-off before writing them.
Files touched (singular dev DB; applied via `just db-load-schema`):
- `singular/db/schema/tb_singular_work_item.sql` — +`current_lease_token`, +`terminal_lease_token`, +`recovery_attempt_count`, +`not_before`.
- `singular/db/schema/tb_singular_work_item_history.sql` — +`lease_token`.
- `singular/db/procs/sp_singular_start.sql` — NEW. PK-insert serializer: `Granted(Fresh)` / `Exists`. Validates + stores app-proposed token on grant (no mint). No prior SELECT.
- `singular/db/procs/sp_singular_resume.sql` — NEW (absorbs reclaim). Existing-work only: `Active`/`Granted(Reclaim|Resume)`/`Deferred`/`Terminal`/`NotFound` + recovery count + `arg_max_recovery_attempts`/`arg_lease_token`. Never inserts.
- `singular/db/procs/sp_singular_complete.sql` — `arg_lease_token` fence, set `terminal_lease_token`, drop owner-authority.
- `singular/db/procs/sp_singular_fail.sql` — **terminal-only** (drop `arg_retryable`), `arg_lease_token` fence, set `terminal_lease_token`.
- `singular/db/procs/sp_singular_extend_lease.sql` — renamed from `renew`; `arg_lease_token` fence, extend-only.
- `singular/db/procs/sp_singular_inspect.sql` — new state enum + `not_before`/`recovery_attempt_count`; no token.
- `singular/db/procs/sp_singular_defer.sql` — NEW.
- `singular/db/procs/sp_singular_indeterminate.sql` — NEW.
- `singular/db/procs/sp_singular_reclaim.sql` — REMOVE (folded into `resume`).
- Shared status/event/result constants (wherever centralized) — extend enum values.

**First review request is PR1's subset** — drafted in
[`pr1-sql-api-diff.md`](pr1-sql-api-diff.md): the schema delta (`current_lease_token` +
`terminal_lease_token` + history `lease_token`) and the `start` / `resume` / `complete` / `fail` /
`extend_lease` diffs that pin start-serializer + same-owner-dup + stale/terminal-token. Hold for
sign-off on those before writing any SQL.

## Open implementation decisions (small)
- `lease_token`: `binary(16)`, **app-minted CSPRNG** (Rev #1), opaque; Singular validates/stores only.
- `max_recovery_attempts`: `resume` argument (authoritative) + optional `item_meta` default.
- `extend_lease`: extend-only (no token rotation, no checkpoint) for the first pass.
- `fail()` is terminal-only (Rev #4); non-terminal retry = `defer` (immediate = `not_before <= now`).
- `READY` **dropped** (Rev v2 #7) — no pre-registration state; "claimable" = no row → `start`, or `DEFERRED` due → `resume`.
- INDETERMINATE: immutable; remediation is a separate ops operation in reference to X.
