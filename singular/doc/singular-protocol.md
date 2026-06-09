# Singular Protocol

**Status:** Normative specification — **target version**, with an explicit
implementation-coverage table (§0.2). Sections marked **(PLANNED)** are not in
the current reference implementation (Singular 0.4) and MUST NOT be relied on
until the coverage table lists them as implemented.

**Scope:** the language-neutral idempotency + lease-coordination contract that
every Singular binding (Drift, Java, Rust, Python, …) and backend (MariaDB
today, others later) must satisfy.

> Singular defines **language-neutral idempotency semantics**. MariaDB stored
> procedures are the first shared backend implementation, chosen so that all
> language bindings can coordinate through **one globally shared authoritative
> state** without independently recreating concurrency, leases, fencing, and
> replay logic.

This document is **normative**. The MariaDB schema/procedures and the Drift
package *implement* it; they are inputs to the specification, not the
specification itself. A future backend may implement the same protocol provided
it passes the conformance suite (§12).

## 0. Layering

```text
Singular protocol        — normative state machine + guarantees (this doc §1–7)
   |
MariaDB backend          — shared authoritative implementation: schema + SPs
   |                        (reference backend mapping, Part II)
Language bindings         — Drift / Java / Rust / Python ergonomic APIs (Part III)
```

Three distinct contracts:

```text
Normative protocol    = what Singular GUARANTEES (every binding + backend)
Backend SP contract   = HOW the reference (MariaDB) backend provides it
Language binding API  = an ergonomic wrapper over a backend contract
```

MariaDB/SP behavior is normative **only where explicitly exposed by the
protocol** (§1–7), not because SQL happens to implement it a certain way.

**Single authoritative state.** All Singular clients in the same idempotency
domain MUST use the same authoritative backend state, regardless of language.
Bindings may expose different idiomatic APIs but MUST converge on the same
logical keys, state machine, leases, fencing tokens, and terminal outcomes.
Separate stores are separate idempotency domains and cannot prevent duplicate
execution across them.

### 0.1 What Singular does and does NOT guarantee

Singular guarantees **outcome-publication authority and durable replay**: at
most one attempt can publish a terminal outcome, and that outcome is replayed
on every later submission of the same key. Singular does **NOT** by itself make
an external side effect exactly-once. **The protected operation must be
independently idempotent** (§4); fencing only decides which attempt's outcome
becomes authoritative, not whether the underlying effect ran once.

### 0.2 Implementation coverage

| Feature | Singular 0.4 (reference) |
|---|---|
| key identity `(service_group, key)`; caller-assigned, stable | implemented |
| `start` (brand-new, PK-serialized grant) | implemented |
| `inspect` (read state/result) | implemented |
| `complete` (fenced → done) | implemented |
| `fail` terminal (fenced → failed) | implemented |
| `extend_lease` (fenced) | implemented |
| `resume` (observe: active / terminal / not-found) | implemented |
| terminal immutability + replay | implemented |
| lease token fencing (capability-token model) | implemented |
| history / audit trail | implemented |
| **expired-lease reclaim** (resume grants a new attempt after expiry) | **REQUIRED — extend 0.4** |
| **`defer`** (working → deferred; durable scheduled re-acquisition) | **REQUIRED — extend 0.4** |
| **input-identity conflict** (same key, different input → conflict) | **PLANNED** |
| **monotonic fence value** (beyond token match) | **PLANNED** |
| **canonical byte serialization of documents** | **PLANNED** |
| **conformance suite / vectors** | **PLANNED (not yet authored)** |

> **Reclaim is REQUIRED**, not optional: if a participant worker crashes
> holding a lease, another worker MUST reclaim the same operation after expiry
> with a new token — otherwise the operation is stuck `working` forever and a
> Microflows poll never progresses. Singular 0.4 must be extended if it cannot
> reclaim. (The first success + lost-ack spike uses only start/complete/inspect
> and does not exercise reclaim; reclaim/defer scenarios follow once the
> capability exists.)
>
> **Defer is NOT failure.** "Processor busy, check again in 5 min" is a
> nonfailure scheduling outcome: no failure and no new logical operation
> occurred, and the same stable key stays active. There is **no
> working→pending "retryable failure"** transition; that case is `defer`
> (§6.7). Terminal `failed` means the operation definitively failed and is not
> auto-retried under that attempt.

> `indeterminate` is **not** a Singular state or outcome. A participant that
> needs it stores it as an **opaque terminal result** via `complete`/`fail`,
> e.g. `done { "kind": "indeterminate", ... }`, and interprets that payload
> itself (§ Appendix A). Singular sees only a terminal with an opaque document.

### 0.3 Required extensions to Singular 0.4 (identify before relying on them)

The target protocol exceeds the current reference SPs. Each gap below MUST be
implemented (and conformance-tested) before any consumer relies on it; until
then the stub/consumer compensates as noted.

| Gap | Current 0.4 | Required change | Stub/consumer workaround until then |
|---|---|---|---|
| **reclaim** | `resume` only observes (Active/Terminal/NotFound) | `resume`/`reclaim` on an expired lease grants a new attempt + rotated token, handing over persisted context (§6.6) | none — required for crash recovery; spike defers reclaim scenarios |
| **defer / deferred state** | absent | new `deferred` state + `defer` op (§6.7); atomic lease release; due-time gating; idempotent by defer command id | none — required for participant "busy, later"; spike defers it |
| **input-identity conflict** | `start` STORES `item_meta` but does NOT compare it; second start ⇒ `Exists` regardless of input | `start` compares canonical input identity; mismatch ⇒ `InputConflict` (§1.3) | **spike: the stub puts the canonical input hash in Singular's `item_meta` on start, reads the original back via `history`/`inspect` on `Exists`, and compares — no second store; returns 409 on mismatch** |
| **terminal replay channel** | replay via `resume`/`inspect`; `start`→`Exists`; foreign-token settle→`TokenStale` (NOT the terminal doc) | unchanged mechanism is acceptable; §3 is normative about immutability, not about which call returns the document | consumer reads terminal outcome via `resume`/`inspect`, not via `start`/foreign `complete` |
| **expiry-aware fencing** | `complete`/`fail` check token only (no expiry) | **consistent with the token-rotation decision (§4)** — no SP change needed; the missing piece is the rotation performed by reclaim/defer | n/a (token-rotation is the agreed rule) |
| **canonical byte serialization** | logical equality | byte-canonical document encoding | compare by value (logical) |

Until a row is implemented + conformance-tested, consumers MUST NOT assume it.

---

# Part I — Normative protocol

## 1. Identity, key scope, idempotency domain

```text
idempotency domain  — one authoritative store. Dedup only WITHIN a domain.
service_group       — a logical partition (namespace) within a domain.
operation key       — caller-assigned, stable, opaque bytes identifying ONE
                      logical operation within (domain, service_group).
input identity      — a caller-supplied descriptor of the input (item_meta),
                      stored with the operation (§1.3).
lease token         — caller-minted capability bytes authorizing mutation while
                      a lease is held (§4).
lease owner         — descriptive worker identity (audit only; never authority).
```

### 1.1 Logical operation identity
Identified by `(service_group, key)`. The key is **caller-assigned and stable
across retries** — never generated by Singular. The same logical operation is
always submitted under the same key.

### 1.2 Key scope
Dedup, replay, and fencing apply within `(service_group, key)` in one domain.
Different keys are independent; different domains cannot dedup against each
other.

### 1.3 Input identity & conflict
A submission carries an input identity (`item_meta`). The first accepted
submission stores it. **(PLANNED)** A later submission of the **same key with a
different input identity** MUST be rejected as `InputConflict`. *In Singular
0.4 the input is stored but not compared:* a second `start` on an existing key
returns `Exists` regardless of input. Until the coverage table marks this
implemented, bindings MUST NOT assume conflict detection.

## 2. State machine

Required states:

```text
working   — under an active, unexpired lease held by a token
deferred  — durably parked until a due time; NO lease owner/token; not
            claimable before defer_until, claimable at/after it (§6.7)
done      — TERMINAL success; carries an immutable result document
failed    — TERMINAL declared failure; carries an immutable error document
```

Plus **expired working** — a `working` operation whose lease has expired. It is
reclaimable (a new attempt may be granted, §6.6) rather than a distinct stored
state.

There is **no `pending` failure state and no `indeterminate` state.**
Application-level indeterminacy is an opaque payload inside a terminal `done`/
`failed` result (§0.2, Appendix A).

Transitions:

```text
(absent)   --start (granted)-->                       working
working    --extend_lease (fenced)-->                 working   (expiry advanced)
working    --complete (fenced)-->                     done      [terminal]
working    --fail (fenced)-->                         failed    [terminal]
working    --defer (fenced, defer_until, context)-->  deferred  (lease released)
working    --lease expiry-->                          (expired-working: reclaimable)
expired-working --resume/reclaim (granted)-->         working   (new attempt+token)
deferred   --resume before defer_until-->             Deferred(defer_until)  (no grant)
deferred   --resume at/after defer_until (granted)--> working   (new attempt+token)
any terminal --any mutation-->                        terminal replay (§3), no effect
```

`done`/`failed` are terminal. Lease expiry performs no transition; the
operation simply becomes reclaimable (§5). **Reclaim is required** (§0.2).

## 3. Terminal immutability & replay

Once `done`/`failed`, the outcome document is **immutable**: no later operation
changes it, and the recorded outcome is retrievable forever. Replays of the
same key yield the **same logical (canonical) outcome** — equal documents,
compared by value. (Byte-identical serialization is **PLANNED**; until then,
equality is logical/canonical, not byte-for-byte.)

**The retrieval mechanism is operation-specific** (do not assume every call
returns the terminal document):

```text
inspect / resume  -> RETURN the terminal result/error (the replay path).
start             -> returns `Exists` (the key already exists); the caller then
                     inspect/resume to read the terminal outcome.
complete / fail with the WINNING token -> idempotent: returns the recorded
                     terminal outcome, no new effect.
complete / fail with a FOREIGN/stale token -> `TokenStale` (NOT the terminal
                     document); the caller has no authority and reads the
                     outcome via inspect/resume instead.
```

So "terminal immutability + replay" is normative; the *channel* differs by
operation. In Singular 0.4 the replay document is obtained through
`resume`/`inspect`, while `start`→`Exists` and a foreign-token settle→
`TokenStale` (§0.3).

## 4. Attempt identity & fencing

```text
lease token  — caller-minted capability bytes, proposed at start. Singular
               stores it only on grant. Every mutator (complete/fail/
               extend_lease/defer) is FENCED by it.
fencing      — a mutation succeeds only if the presented token matches the
               CURRENT stored token; otherwise TokenStale (§10): the caller has
               lost AUTHORITY and MUST suppress effects and stop.
```

**DECISION — token rotation fences, not wall-clock expiry.** A mutation is
authorized by **matching the current stored token**, not by the lease being
unexpired. Wall-clock lease expiry alone does **not** revoke authority; it only
makes the operation **reclaimable**. Authority is revoked by **token
rotation**, which happens on: `reclaim` (grants a new token to a new attempt),
`defer` (invalidates the token immediately), or a new `start` grant. Until the
token is rotated, the original holder may still publish.

Rationale: this satisfies the crash-recovery contract (a dead worker never
publishes; a live reclaimer rotates the token and fences the prior holder; a
revived original finds its token stale) without forcing a worker that finished
exactly at expiry to reclaim before completing. The current Singular 0.4
`complete`/`fail` (token-match only, no expiry check) is **consistent** with
this decision; the gap is that `reclaim`/`defer` (which perform the rotation)
do not yet exist (§0.3). **Protocol, SPs, and conformance tests MUST all agree
on this rule.**

Fencing guarantees **outcome-publication authority**: at most one attempt's
terminal outcome becomes authoritative. It does **not** guarantee the protected
external effect ran exactly once — that effect must be independently idempotent
(§0.1). A stale worker may keep computing but can never publish.

**(PLANNED)** A monotonic fence value (beyond token equality) for mutual
exclusion against non-Singular effects.

## 5. Lease semantics

```text
a granted lease has an explicit expiry = backend-sourced now + positive
  timeout (seconds). Time is backend-sourced, never a worker wall clock (§7).
while unexpired, only the token holder may mutate (§4).
extend_lease (fenced) advances expiry; never changes state or result.
on expiry: no automatic transition; the operation becomes RECLAIMABLE.
  Reclamation via resume grants a fresh attempt + token, fencing out the prior
  token (REQUIRED, §6.6). A worker that wants to wait WITHOUT holding a lease
  uses defer (§6.7) rather than letting the lease lapse.
```

## 6. Operations

Each: required inputs, allowed prior states, transition, outcomes, replay,
concurrency/fencing, failure.

### 6.1 start(key, input_identity, lease_owner, lease_timeout, lease_token)
- **Inputs:** key; input_identity (item_meta object); 16-byte lease_owner;
  positive lease_timeout; 16-byte lease_token.
- **Prior states:** absent.
- **Transition:** absent → working (granted).
- **Outcomes:** `Granted(lease)` | `Exists` | terminal replay (if terminal).
  **(PLANNED)** `InputConflict`.
- **Replay:** existing & working ⇒ `Exists`; existing & terminal ⇒ replay.
- **Concurrency:** the key is the serializer; exactly one concurrent first-
  submit wins, others get `Exists`.
- **Failure:** bad token length / non-positive timeout / malformed input ⇒
  `InvalidInput`, no state change.

### 6.2 inspect(key)
- **Inputs:** key. **Prior states:** any. **Transition:** none.
- **Outcomes:** `Working(owner, expires, checkpoint?)` | `Terminal(result|error)`
  | `NotFound`.
- **Replay:** idempotent read; never blocks a mutator; observes published state.

### 6.3 extend_lease(key, lease_token, new_timeout)  [fenced]
- **Prior states:** working (held). **Transition:** working → working.
- **Outcomes:** `Extended` | `TokenStale` | `Terminal(result|error)` |
  `NotFound`.
- **Failure:** invalid timeout ⇒ `InvalidInput`.

### 6.4 complete(key, lease_token, result)  [fenced]
- **Prior states:** working (held). **Transition:** working → done.
- **Outcomes:** `Settled(result)` | `TokenStale` | terminal replay | `NotFound`.
- **Replay:** already-terminal ⇒ recorded outcome, no re-effect.
- **Concurrency/fencing:** stale token ⇒ `TokenStale`, suppress.
- **Note:** application-level indeterminacy is carried here as an opaque result
  document, e.g. `{ "kind": "indeterminate", ... }` (§0.2, Appendix A).
- **Failure:** malformed result ⇒ `InvalidInput`.

### 6.5 fail(key, lease_token, error)  [fenced]
- **Prior states:** working (held).
- **Transition:** working → failed (TERMINAL, definitive).
- **Outcomes:** `Settled(error)` | `TokenStale` | terminal replay | `NotFound`.
- **Replay:** terminal failure replays the recorded error.
- **Note:** there is NO retryable failure. A "try again later" situation is
  `defer` (§6.7), not `fail`. `failed` means definitively failed; it is not
  auto-retried under that attempt.
- **Failure:** malformed error ⇒ `InvalidInput`.

### 6.6 resume / reclaim(key[, lease_owner, lease_timeout, lease_token])
- **Inputs:** key; for a granting reclaim, a fresh lease_owner + positive
  timeout + caller-minted lease_token.
- **Prior states:** any.
- **Transition:**
  - terminal ⇒ none (replay);
  - `working` (live lease) ⇒ none — report `Active(expires)`, never steal;
  - `deferred` before `defer_until` ⇒ none — report `Deferred(defer_until)`;
  - **expired-working** OR `deferred` at/after `defer_until` ⇒ granted: a fresh
    attempt (`Granted(lease)`) with a new token, fencing out the prior token,
    handing the new attempt any persisted context/checkpoint.
- **Outcomes:** `Granted(lease)` | `Active(expires)` | `Deferred(defer_until)` |
  `Terminal(result|error)` | `NotFound`.
- **REQUIRED:** reclaim after expiry is mandatory (§0.2). Singular 0.4 must be
  extended if it cannot grant a new attempt on an expired lease.
- **Concurrency:** after the deadline/expiry, **exactly one** contender wins
  the new lease; losers observe the granted state.

### 6.7 defer(key, lease_token, defer_command_id, defer_until, context)  [fenced]  — REQUIRED
A nonfailure scheduling outcome: the worker cannot finish now and durably
parks the operation until a due time. Not success, not failure.
- **Inputs:** key; held lease_token; **`defer_command_id`** (a stable id for
  THIS defer command — see replay below); `defer_until` (a due time on the
  shared backend clock); optional `context`/checkpoint to hand the next
  attempt.
- **Prior states:** working (held).
- **Transition:** working → deferred. The current lease is **atomically
  released**; the prior token is invalidated immediately after commit (the
  defer itself is the token rotation, §4).
- **Outcomes:** `Deferred(defer_until)` | `TokenStale` | `DeferConflict` |
  terminal replay | `NotFound`.
- **Command identity & replay (Finding 4).** Because defer invalidates the
  lease token, a naive retry of the same defer with the now-stale token would
  read as `TokenStale` — wrong for an idempotent retry. So defer is keyed by a
  stable **`defer_command_id`**, not by the (now-rotated) token:
  - first commit binds `(key, defer_command_id) → (defer_until, context)`;
  - a **replay with the same `defer_command_id` AND the same `defer_until` +
    `context`** is idempotent: returns `Deferred(defer_until)`, no new effect,
    even though the token is now stale;
  - a replay with the same `defer_command_id` but a **different `defer_until`
    or `context`** is a `DeferConflict` (the command is not re-interpretable);
  - a **different** `defer_command_id` arriving with a stale token (the lease
    was already released by the first defer) is `TokenStale` — it is a new
    command without authority.
- **Properties:**
  - no worker can acquire before `defer_until`; at/after it, exactly one
    contender acquires a new lease (§6.6);
  - the new attempt receives the persisted `context`/checkpoint;
  - time comparisons use the shared backend clock (§7);
  - Microflows MAY use `defer_until` to schedule its next reconciliation poll,
    but **Singular authoritatively prevents early participant execution** —
    the schedule is an optimization, the lease gate is the guarantee.
- **Failure:** non-positive/invalid `defer_until`, malformed context, or
  missing `defer_command_id` ⇒ `InvalidInput`.

## 7. Determinism: clock, IDs, retry

```text
clock — backend-sourced; never a worker wall clock; never an ambient clock
        inside backend transition logic.
IDs   — keys and lease tokens are caller-supplied/minted and stable; Singular
        does not auto-generate identity that influences outcome.
retry — replaying the same key + same input yields the same logical outcome
        (§3). Values influencing committed behavior are fixed across retries.
```

## 8. Concurrency, lost-response recovery, history, security (cross-cutting)

**Concurrency.** The key serializes first-submit (exactly one grant);
mutations are fenced (only the current token publishes); `inspect` never blocks
and reads published state; each mutation is one atomic backend unit (§11).

**Lost-response recovery.** If a caller does not learn whether a mutation
committed (crash, lost ack), it recovers by **re-inspecting / re-submitting
under the same key** — never a new key. Committed terminal outcomes replay
unchanged; an uncommitted mutation is simply re-attempted under the same fenced
token or observed as still-working. A caller can always resolve "did it
happen?" by key.

**History / retention.** A backend SHOULD keep an append-only history of
state-affecting events sufficient to audit lifecycle and prove terminal
immutability. A backend MUST NOT mutate or delete a terminal outcome while the
key may still be replayed; retention duration is a deployment policy.

**Security & sensitive data.** `key`, `item_meta`, `result`, `error` may be
sensitive; treat as confidential at rest and in transit. `lease_owner` is
descriptive and MUST NOT be an authorization check; authority is the lease
token. Lease tokens are capabilities — unguessable (CSPRNG-minted), never
logged in cleartext. Per-attempt diagnostic traces MUST be distinguished from
committed audit records and MUST NOT leak tokens or sensitive payloads.

## 9. Canonical serialization & schema/version

```text
documents (item_meta, result, error, checkpoint) are JSON OBJECTS: non-null,
  valid, object-typed; the empty document is `{}`, never null.
equality is logical/canonical (compare by value). Byte-canonical serialization
  is PLANNED; until then bindings MUST NOT assume byte-identical replay.
monetary / exact values are typed integers or fixed-scale, never floating point.
each document is governed by a schema + version negotiated out of band; the
  backend stores them OPAQUELY and MUST NOT reinterpret their meaning.
```

## 10. Error taxonomy

```text
Granted / Exists / Active / Deferred / Settled / Terminal / NotFound — normal.
TokenStale          — lost fence/authority; suppress effects, stop.
DeferConflict       — same defer_command_id, different defer_until/context (§6.7).
InvalidInput        — bad key / token length / timeout / malformed document.
BackendUnavailable  — could not reach/acquire the store (retriable).
BackendTimeout      — a deadline elapsed (retriable / shed load).
BackendRejected     — store ran the op and reported failure (not blindly retriable).
BackendResponseInvalid — malformed/unexpected backend response (schema/version skew).
(PLANNED) InputConflict — same key, different input identity.
```
(`Deferred(defer_until)` is a nonfailure scheduling outcome, §6.7. There is no
"requeued/retryable failure" outcome.)

`detail` strings are diagnostic only, never a compatibility contract.

## 11. Backend transaction assumptions

A conforming backend MUST provide: **atomic mutation** (each
start/complete/fail/extend_lease commits state + token + expiry + outcome +
history as one unit or not at all); **first-submit serialization** (a
uniqueness mechanism on `(service_group, key)`); **fenced mutation** (token/
expiry/state check and publication in the same atomic unit); **read of
published state** for `inspect`.

---

# Part II — Reference backend: MariaDB stored procedures

Normative **for the MariaDB reference backend**; other backends substitute
their own Part II while preserving Part I.

## Mapping: protocol operation → stored procedure

```text
start        -> sp_singular_start
inspect      -> sp_singular_inspect
extend lease -> sp_singular_extend_lease
complete     -> sp_singular_complete
fail         -> sp_singular_fail
resume       -> sp_singular_resume
history      -> sp_singular_history
```

**Call order.** (1) optional `inspect` fast path; (2) `start` for brand-new
(PK insert serializes), `Exists` ⇒ `resume`; (3) while working: `extend_lease`,
then `complete`/`fail`; (4) after ambiguity: `resume` to observe.

**Parameters.** Explicit `service_group`, `key`, JSON-object document args,
16-byte descriptive `lease_owner`, positive `lease_timeout_seconds`, 16-byte
capability `lease_token`. Time is taken from the backend at the SP boundary and
written to lease expiry / history.

**Result documents.** One result set, one row, one column `result`: a
discriminated JSON object keyed by `outcome`, arm-inapplicable fields omitted
(never null). Numeric codes / SP names / SIGNALs stay backend-private.

**Transaction behavior.** Each SP does projection + history writes in one
transaction (the atomic unit of §11). The PK on `(service_group, key)` is the
first-submit serializer; mutators validate token/expiry/state and publish in
the same transaction (fencing).

**Error mapping.** Input/contract `SIGNAL '45000'` → `InvalidInput`;
corruption signal (distinguished errno, e.g. 30001) → `BackendResponseInvalid`;
transport/pool → `BackendUnavailable`/`BackendTimeout`; other server errors →
`BackendRejected`.

The Drift gateway + `sp_singular_*` are the current reference realization;
inputs to this spec, not the spec.

---

# Part III — Language binding API conventions

A binding is an **ergonomic wrapper** over a backend contract. Bindings may
differ in naming and error representation (exceptions vs result types) but MUST:
expose the operations (§6) with the protocol outcomes (§10); treat documents as
canonical JSON objects (§9); never use `lease_owner` as authority and fence
every mutator by token (§4); source time from the backend (§7); preserve
terminal immutability + logical replay (§3); pass the conformance suite (§12).
Bindings MUST NOT add semantics the protocol does not guarantee, nor depend on
SQL-API artifacts the protocol does not expose. The Drift binding
(`singular/drift/`) is the first reference binding.

---

# Part IV — Conformance

## 12. Conformance suite / protocol vectors  (PLANNED — not yet authored)

A shared, backend- and language-neutral suite will prove every binding+backend
behaves equivalently against the same scenarios. Each vector is expressed in
protocol operations (§6) and asserts protocol outcomes (§10) + state (§2), not
SQL or any binding's API. It will live under `singular/tests/conformance/` as
language-neutral vectors plus a thin per-binding driver.

Planned vectors (initial set; those depending on PLANNED features are tagged):

```text
V1  start brand-new                       -> Granted(working)
V2  concurrent start                      -> exactly one Granted, rest Exists
V3  complete (fenced)                     -> done; inspect -> Terminal(result)
V4  complete replay (same key)            -> same logical result, no re-effect
V5  fail terminal                         -> failed; replay -> same error
V6  extend_lease (fenced)                 -> Extended; expiry advanced
V7  mutate with stale token               -> TokenStale; no effect
V8  inspect NotFound                      -> NotFound
V9  terminal immutability                 -> no mutator changes a terminal outcome
V10 lost-ack recovery (start/complete/    -> re-inspect/re-submit by key yields
    inspect)                                 the authoritative outcome once
--- REQUIRED behaviors (extend 0.4 where missing) ---
V11 worker dies, lease expires, reclaim   -> new worker granted new token; old
                                             token fenced; operation completes once
V12 defer(defer_until, context)           -> deferred; resume before due ->
                                             Deferred; resume at/after due ->
                                             Granted with context; old token stale
V13 defer idempotent replay               -> same deferred outcome, no new effect
--- depend on PLANNED features ---
V14 (PLANNED) same key + different input  -> InputConflict
V15 (PLANNED) opaque indeterminate result -> done{kind:indeterminate} replayed
```

V1–V10 use only Singular 0.4 capabilities and suffice for the first
Microflows success + lost-ack spike. V11–V13 (reclaim + defer) are required
participant-contract behaviors and gate the crash-recovery and defer spike
scenarios; they may require extending Singular 0.4.

---

## Appendix A — relationship to Microflows (indeterminate mapping)

Microflows (a separate product) consumes Singular at the participant side: a
participant service uses Singular to make its operations idempotent and to
replay durable outcomes. Microflows itself does not depend on Singular.

**Indeterminate is a Microflows concept, not a Singular state.** When a
participant's underlying effect is genuinely undetermined, it records that as
an **opaque Singular `done` result** — e.g. `complete(key, { "kind":
"indeterminate", "detail": ... })`. Singular stores and replays that document
opaquely. The participant decodes it and maps it onto the Microflows REST
protocol's `indeterminate` outcome, which Microflows then persists as its own
`blocked_resolution` workflow state. Singular never interprets or resolves it.
