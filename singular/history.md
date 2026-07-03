## 2026-07-03 – 0.8.0: RpcCommitError compatibility (mariadb-rpc kind/cause_tag redesign)

Bumped from 0.7.0 (source changed; 0.7.0 predates this work and was already an existing version, so
reusing it would have meant re-signing an already-sealed number for new content).

Upstream `drift-mariadb-client` redesigned `RpcCommitError` from a `tag`/`message` `pub error` into a
plain struct — `kind: RpcCommitErrorKind` (`AmbiguousWrite` / `NotSent` / `ServerRejected`) plus
`cause_tag: String` for diagnostics only, documented as "consumers branch on `kind`". `gateway.drift`'s
`_finish_stmt_and_commit` hadn't caught up:

- First pass (compat-only) read `e.tag`, which no longer exists — replaced with `e.cause_tag`, but still
  collapsed every commit failure into `BackendRejected`.
- Follow-up (review finding, high severity): that collapse was semantically wrong.
  `AmbiguousWrite`/`NotSent` are retriable/reconcile-safe (the write may not have applied, or definitely
  didn't); only `ServerRejected` (server alive, definitively did not commit) is a hard rejection. Now
  matches on `e.kind`: `ServerRejected` → `BackendRejected`, `AmbiguousWrite`/`NotSent` →
  `BackendUnavailable`.

Verified: full root `just test` green (61/61) against a refreshed `mariadb-rpc` package pool (the locally
cached one predated the upstream redesign and couldn't validate the fix on its own).

## 2026-06-06 – PR1: Singular/Microflows protocol hardening (minimal safe protocol)

PR1 establishes the minimal safe Singular/Microflows task protocol — "at-least-once
attempts, effectively-once effects" — first on drift 0.33.24 (abi15). Landed on the `main`
working tree (SQL + gateway + tests, green at 16/16 under `just test`). PR1 deliberately
does **not** implement recovery grants; that is PR2.

> **Re-anchored 2026-06-08 to drift 0.33.26 / abi 16** (relock + author-claim re-mint; no
> abi15→16 source breakage, still 16/16). Context: Singular + Microflows are being handed off to a
> dedicated repo folded into the toolchain/cert. The **determinism reshape** (every transition a
> pure function of state + explicit args; caller-supplied `std.time.UtcTimestamp`; no DB clock; thrown
> `EventTimeConflict`; explicit lease expiry; drop `UTC_TIMESTAMP`/`+INTERVAL`) is **designed + locked
> but parked** for the new repo — full design in
> `work/singular-microflows-protocol-hardening/implementation-plan.md`. It depends on the µs
> `std.time` that arrived in 0.33.26.

### Claim split

- Split claiming into two intents:
  - `start(...)` — brand-new work only; the database PK `(service_group, idempotency_key)`
    is the serializer (one winner `Granted`, the rest `Exists`). Explicit `HANDLER FOR 1062`,
    not `INSERT IGNORE`, so only a real PK conflict is `Exists`.
  - `resume(key)` — observe existing work (`Active`/`Terminal`/`NotFound`) without granting
    recovery yet. PR1 is tokenless; a token-carrying recovery request returns in PR2.

### Capability-token authority + fencing

- Replaced worker *identity* as authority with an app-minted 16-byte lease **capability
  token**. `lease_owner` is descriptive/audit only, never an authority check.
- Fenced `complete`, `fail`, and `extend_lease` against stale/foreign workers via the token.
- Preserved the terminal-writer token (`terminal_lease_token`) so a retry can recover and
  redeliver the authoritative result.

### Actionable-state terminal contract

- Unified terminal mutation to one outcome (no `Duplicate`, no Applied/Terminal split):
  - `Settled(result)` → deliver the authoritative result to Microflows.
  - `TokenStale` → suppress callbacks, exit.
  - `NotFound` → integration/protocol failure, alert.
- Correct cross-terminal handling: `complete()` on a FAILED item returns the recorded
  failure; `fail()` on a DONE item returns the recorded success — never a false outcome.
- Terminal replay is active recovery: a replacement node (`start→Exists→resume→Terminal`)
  repairs the commit-before-callback crash window and redelivers the stored result **without
  repeating the external effect**.
- `extend_lease` never shortens a live lease (floors the new expiry at the current one).
- Live leases are always bounded: `start`/`extend_lease` reject a non-positive timeout
  (gateway `InvalidLeaseTimeout`, SP `SingularLeaseTimeoutInvalid`), and a schema CHECK
  enforces `WORKING ⇒ lease_expires_at IS NOT NULL` — an unbounded lease could never be
  reclaimed.

### Correctness of the backend boundary

- Enforced the JSON object-document contract on terminal payloads at both boundaries: a payload
  must be a non-NULL JSON **object** (not empty / JSON null / array / scalar). The SP `SIGNAL`s and
  the gateway raises caller-input `InvalidJson(field)`; an absent document is `{}`, never JSON null.
- Strict backend-response decoding: a malformed/missing required field (terminal payload,
  required history fields) becomes `BackendResponseInvalid`,
  never a silent default delivered as authoritative data.
- Distinguishes a missing projection (NotFound) from a dangling head-history pointer
  (corruption): every SP reads the projection first, then checks the referenced head row
  explicitly and `SIGNAL`s `SingularHeadHistoryMissing` (SQLSTATE 45001 → gateway
  `BackendResponseInvalid`) rather than inferring state from NULL.
- MariaDB-specific rows, numeric result codes, SP names, SIGNALs, and diagnostics are mapped
  behind domain-level gateway variants — callers branch on the domain, never on SQL codes.

### Surface reduction

- Public gateway result/data surface trimmed ~19 → 9 types (removed single-field result
  wrappers, `TerminalState`, `ActiveInfo`, `WorkKind`, public `WorkState`, `Inspection`,
  `HistorySummary`; `same_owner`).
- `history()` retained as a validated, raw audit surface (event-only; the `(event_type,
  status)` pair validated adapter-privately).

### Discriminated single-JSON-result + JSON object-document contract

- Replaced the positional SP result sets (which still carried nullable `status_code` /
  token / `lease_expires_at` primitives that map poorly to host languages and admit
  invalid field combinations) with ONE discriminated JSON document per actionable SP:
  every `start`/`resume`/`complete`/`fail`/`extend_lease`/`inspect` returns a single
  `result` column — a JSON OBJECT keyed by `outcome` (`granted`/`exists`/`active`/
  `terminal`/`settled`/`token_stale`/`not_found`/`working`), with arm-inapplicable fields
  OMITTED rather than emitted as SQL/JSON null. No nullable primitive crosses the boundary;
  the numeric storage codes / SP names / SIGNALs stay backend-private.
- Established the DB JSON **document contract**: every JSON value (SP inputs, persisted
  columns, and result documents) is a non-NULL JSON **object** — the empty document is
  `{}`, never SQL NULL / JSON null / top-level array / top-level scalar (arrays NESTED in an
  object are fine). Enforced three ways: schema `CHECK (json_valid(c) AND
  json_type(c)='OBJECT')` + `NOT NULL`; SP `SIGNAL` on bad inputs; and the gateway
  validates the SAME object contract on BOTH sides — before SQL (caller input →
  `InvalidJson`) and on decode (backend response → `BackendResponseInvalid`).
- Gateway decode (`_read_result_doc`/`_doc_*`/`_terminal_from_doc`): payload and checkpoint are
  NESTED JSON objects (document semantics — not JSON-in-a-string), re-encoded compact for
  delivery (`json.encode_compact`); the stored event record stays immutable. `lease_owner` rides
  as `LOWER(HEX())` and is decoded via `codec.hex_decode`; `start` threads the caller's own
  validated input token rather than re-decoding it from the result. Public gateway variants UNCHANGED.
- Regressions: e2e scenario 16 (gateway before-SQL contract — null/array/scalar/malformed/
  empty rejected, `{}`/populated/nested-array accepted, no mutation on reject); the
  `singular_malformed` fixture rebuilt to drive the DECODE-side contract (envelope + nested
  payload + owner-hex/checkpoint, accept + reject); and SP-invariant cases for the SP-input
  object contract on `start` item_meta and `complete` payload.
- Review follow-ups (same day): `lease_owner` is `NOT NULL` and SP params are `varbinary(16)`
  validated to exactly 16 bytes (`SingularLeaseOwnerInvalid`) — no silent zero-pad, no JSON-null
  owner. PR1 `resume` is non-locking (dropped its `FOR UPDATE`; it is read-only until PR2 can
  grant/reclaim). `history()` transport was reshaped to the document contract too: each event is a
  JSON object whose `event` field is the discriminator (no `outcome`); meta/payload/checkpoint
  nested; `lease_expires_at` omitted for non-lease events; `Array<HistoryEntry>` is unchanged
  publicly (raw-audit semantics preserved, one DB boundary).
- Event/status corruption validation: a schema CHECK (`ck_singular_history_event_status`) makes a
  mismatched (event_type, status) pair unrepresentable (CLAIMED/EXTENDED→WORKING, COMPLETED→DONE,
  FAILED→FAILED); history transports `status` so the gateway re-cross-checks it against `event` on
  decode (then drops it). Pinned by an SP-invariant CHECK test + a malformed-history fixture row.
  lease_owner is NOT NULL + 16-byte-validated (owner-input regressions on start/complete/fail/extend).

### Certification

- Added regressions to the normal cert gate (`just test`): malformed-backend (isolated
  `singular_malformed` fixture, with valid controls so it can't pass vacuously),
  token-fencing, start/complete concurrency races, terminal replay-vs-stale, cross-terminal,
  never-shorten lease extension, and token/JSON input validation.
- Added a raw-SQL/SP-invariant track (pymysql) that drives the SPs directly — cases the typed
  gateway can't express: SQL NULL/zero/negative timeouts (SIGNAL + projection/history counts
  unchanged), dangling head-history corruption on all five SPs (errno 30001 →
  `BackendResponseInvalid`), and the missing-projection→NotFound control. It runs in the cert
  gate as a serialized `DB_GROUP` job via the same executor (per-run nonce isolation, exact
  cleanup); `just test-sql` is the standalone dev runner.

### Deferred to PR2+

- Recovery grants via `resume`, `WorkMode { Fresh, Recovery }` (a DIRECT signal — never
  inferred from checkpoint emptiness), `WorkLease.checkpoint_json`, `DEFERRED`/`defer`
  (PR3), `INDETERMINATE` (PR4), and recovery-attempt accounting + exhaustion.
- **Determinism reshape** (caller-supplied timestamps, no DB clock) — designed + locked,
  parked for the new repo on the µs `std.time.UtcTimestamp` (0.33.26+); see
  `work/singular-microflows-protocol-hardening/implementation-plan.md` ("Determinism reshape (PARKED)").
