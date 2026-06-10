# generic-rest-dispatcher  (milestone-1 step 9)

## Short-term objective
Replace the runner's hardcoded echo-transform behavior with a generic dispatcher
driven entirely by the **persisted operation request** and **trusted participant
configuration** — never workflow input. Reversal/compensation (step 6) reuses it.

## Current behavior / problem
The runner (`microflows/runner/src/runner.drift`) is a manual-IR driver for ONE
operation, `echo-transform`, of script `protocol_spike` rev 1. The dispatch path
itself (`_op_url`/`_dispatch_put`/`_classify_dispatch`/`_reconcile`/`_extract_result`)
is ALREADY generic over `(operation_name, input, base_url)` and emits normalized
protocol envelopes. What is hardcoded / missing:
- routing target is a single `--participant-url` CLI arg, not a config registry;
- the operation request carries no `schema_version` (the table lacks the column);
- only one participant operation exists, so "generic" is unproven;
- the initial request is derived from CLI; only *recovery* reads durable state.

## Accepted design decisions
- **DSL/compiled IR and deployment config are SEPARATE concerns** (per direction).
  - `operations` map = a TEMPORARY manual-IR registry shaped like future compiler
    output (operation → `{participant, schema_version}`; later: input/result types,
    compensation binding). Not permanent deployment config.
  - `participants` map = the LASTING deployment boundary: a logical participant id
    → environment-specific transport policy.
- **Config shape (this slice) — arrays of records so the WHOLE registry is
  iterable/validatable at startup** (Drift stdlib has no clean JSON object-key
  enumeration; the codebase iterates arrays by index):
  ```json
  { "db": {...},
    "participants": [
      { "id": "ref",
        "transport": { "kind": "http", "endpoints": ["http://127.0.0.1:PORT"],
                       "selection": "ordered_failover" },
        "auth_profile": null } ],
    "operations": [
      { "name": "echo-transform", "participant": "ref", "schema_version": 1 } ] }
  ```
  Explicit `id`/`name` make duplicate detection real.
- **Participant resolution behind an ABSTRACTION** so richer endpoint/credential
  policies (pools, failover/random selection, health, timeouts, TLS, proxies,
  credential-provider refs) never force dispatcher changes. This slice: one HTTP
  endpoint, ordered selection, no auth (or the narrow required mechanism).
- **Dispatch is a pure function of durable state + resolved transport.** At
  dispatch the runner reads `operation_name, schema_version, input_json,
  input_hash, operation_id` from the persisted request (`operation_request_get`)
  and resolves transport from config — never from CLI/workflow input. Endpoint
  retries always dispatch the SAME stable operation id + identical request.
- **Startup validation (fail BEFORE execution):** unknown participant reference,
  unsupported transport `kind`/`selection`/auth, empty endpoint list, invalid URL,
  duplicate operation binding.
- **Invariants:** never derive routing or credentials from workflow input; never
  persist credentials in operation requests. `--participant-url` is removed.
- **`schema_version`** added to `tb_mf_operation` + `sp_mf_operation_request`(`_get`)
  + host `OperationRequestRecord` + runner (design §2.5). Sourced from the
  `operations` registry at submit; read from durable state on dispatch/recovery.
- **Singular stays behind the participant boundary** — the dispatcher depends only
  on the REST contract (already true).
- **Second operation type proves genericity:** add `string-join` to the stub
  (join an array of strings → `{"joined": "..."}`), distinct in shape from
  echo-transform's `{"sum": N}`, with its own validation + exec-count.

## Concrete implementation plan
1. Schema: add `schema_version int NOT NULL` to `tb_mf_operation`; thread through
   `sp_mf_operation_request` + `sp_mf_operation_request_get`; extend SP regression.
2. Host: `operation_request` takes `schema_version`; `OperationRequestRecord`
   carries it.
3. Runner — config: parse `participants` + `operations` registry; resolve
   `(base_url, auth)` for an operation; drop `--participant-url`.
4. Runner — dispatch: read the operation entirely from the durable request;
   apply config auth headers on PUT/GET; persist `schema_version`.
5. Stub: add the `string-join` operation (route, validate, body, exec-count);
   keep `echo-transform`.
6. Tests: integration harness — add a `string-join` dispatch case proving generic
   dispatch; retain ALL lost-ack / restart / idempotency / rejection regressions.
7. Verify: SP regression, integration suite (≥13 incl. the 2nd op), full root test.

## Files likely affected
- `microflows/db/schema/tb_mf_workflow_operation.sql`
- `microflows/db/procs/sp_mf_operation_request.sql`, `sp_mf_operation_request_get.sql`
- `microflows/db/tests/sp_operation_test.py`
- `microflows/packages/microflows/src/host.drift`
- `microflows/runner/src/runner.drift`
- `microflows/participant-stub/src/app.drift`
- `integration/coordinator-singular/test.py`

## Verification criteria
- A NON-echo operation (`string-join`) dispatches end-to-end through the same
  loop and completes — proving dispatch is data-driven, not renamed hardcoding.
- All current properties still pass (normal success, lost-ack recovery, idempotent
  re-run, effectively-once, pending defer, rejection→blocked, durable-request
  recovery, inconsistent-terminal). `just test-integration` green; SP 28→ updated.

## Current status and next action
**In progress — data layer DONE & verified.**
- [x] Step 1: `schema_version` column on `tb_mf_operation`; threaded through
  `sp_mf_operation_request` (param + immutable-identity conflict check + insert)
  and `sp_mf_operation_request_get` (output). SP regression 29/29 (added
  `request_schema_version_conflict`).
- [x] Step 2: host `operation_request` signature + `OperationRequestRecord` carry
  `schema_version`.
- Decision: config uses **arrays-of-records** for `participants`/`operations` with
  **whole-registry startup validation** (Drift stdlib lacks clean JSON object-key
  enumeration). Resolution behind an abstraction; ordered selection; no auth this
  slice (non-null `auth_profile` → unsupported → fail).

- [x] Steps 3–4: runner refactor — **compiles from source.** Parses + validates
  the WHOLE registry at startup (`_validate_registry`: duplicate ids/names,
  unknown participant refs, unsupported `kind`/`selection`, empty endpoints,
  invalid URLs, unsupported `auth_profile`, bad `schema_version`); resolves an
  operation → transport behind `_resolve_operation`/`ResolvedOperation` (ordered
  selection = first endpoint); dropped `--participant-url` (now `--operation`
  required); threads `schema_version` (fresh ← registry, resume ← durable);
  dispatches to the resolved `base_url`.

- [x] Runner review round 1 (all compile):
  - **High** — resolution honors the PINNED contract version on resume:
    `_resolve_operation(cfg, op_name, pinned)` fails safely if the registry no
    longer offers the durable `schema_version` (no dispatching a v1 request
    through a v2 binding).
  - **Medium** — `--operation` is now optional; required only for a FRESH
    workflow. A resume needs only the workflow id (durable request supplies it).
  - **Medium** — `auth_profile` validated via `ap.is_null()` directly, so every
    non-null JSON type (bool/number/array too) is rejected, not just object/string.

- [x] Runner review round 2 (lease safety; compile-clean):
  - **High** — fresh-path preconditions (missing/unresolvable `--operation`) are
    rejected BEFORE create/claim, so a bad submission takes no lease.
  - **Medium** — a resume that can't resolve releases/blocks instead of holding
    the lease until expiry.
- [x] Runner review round 3 (compile-clean):
  - **High** — probe/claim race: added an AUTHORITATIVE post-claim reload of
    `operation_request_get`. The pre-claim probe is best-effort (fast-reject only);
    after claiming we re-read, and if a request was persisted between probe and
    claim we adopt it (flip to resume) so CLI-derived fields can't cause a spurious
    `operation_conflict`.
  - **Medium** — `operation_unresolvable` is now DURABLY recorded: instead of an
    in-process-only re-defer loop (release writes no event), it transitions to
    `blocked_resolution` via `operation_fail` (clears the lease + appends an audit
    event with the reason), so inspection explains the stall and the loop stops.

- [x] Runner review round 4 (compile-clean; SP 33/33):
  - **High+Medium** — an unavailable pinned binding is a REPAIRABLE config state,
    not a failure: replaced the `operation_fail` (blocked_resolution) path with a
    dedicated **`sp_mf_operation_dispatch_defer`** transition. It keeps state
    forward + status requested + continuation, clears the lease, sets a future
    `next_attempt_at`, and appends an `operation_dispatch_deferred` audit event
    (reason `pinned_contract_unavailable`) on ENTRY into the wait (deduped on
    retry — verified: 1 event across re-claims). Config repair auto-recovers it on
    the next poll. New host `defer_dispatch`; runner `_defer_dispatch`.
  - **Medium** — omitted-`--operation` race: restructured so a fresh submission
    validates `--operation` before create/claim (no lease), a resume-by-id does
    NOT pre-reject (claim is authoritative: `NotFound` ⇒ nothing to resume, no
    lease), and the post-claim reload is authoritative. A claimed workflow with no
    request and no `--operation` durably defers (`operation_request_absent`).

**Next:** stub `string-join` op (5); harness array-config + `string-join` case,
keep all 12 regressions (6); build both from source + full verify (7).

## Open questions / blockers
- Auth scheme shape (header/value vs bearer) — starting with a single
  `{header, value}` pair the stub accepts; revisit if a real participant needs more.
- Participant registry lives in the runner CONFIG for milestone 1; a durable
  participant/operation-definition table is deferred (design §7 step 2b / parser).

## Relevant roadmap
- Milestone-1 step 9 (design §7). Gated on "after the spike proves the model" —
  proven by the coordinator<->singular integration suite. Step 6
  (reversal/compensation) follows and reuses this dispatcher.
