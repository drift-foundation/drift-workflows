# uflowsd — Participant & Manifest Contract (as-built)

**Status:** as-built, conformance-pinned. Every rule here is enforced by `microflows/runner` and exercised
by the integration gate. Verified against `main@76032d4`, certified driftc 0.33.61 / abi 18.

**Supersedes** the *proposed* participant protocol in `microflows_design.md §5` (still marked "open for
redesign"). Where §5 and this document disagree, **this document is authoritative** — §5 describes an
intended superset, parts of which are not built (see [Appendix A](#appendix-a--what-design-5-proposes-that-is-not-built)).

**Audience:** teams that (a) deploy workflows on `uflowsd`, or (b) implement a **participant service** that
`uflowsd` drives (e.g. a Bookkeeper participant). If you are implementing a participant, the section you need
is [§4 Participant HTTP contract](#4-participant-http-contract-what-you-implement).

File:line anchors point at the enforcing code so this doc stays checkable as the source evolves.

---

## 0. Transport neutrality (governing constraint)

The canonical contracts here are two **transport-neutral documents**:

- the **workflow-outcome document** — `{ "workflow": completed | failed | blocked | …, "reason", "compensated", "result" }`
- the **participant-outcome vocabulary** — `result-produced | pending | rejected | conflict | no-record | uncertain`

Every transport — HTTP today, pipes/filesystem later — is a thin **adapter** that maps these documents to its
native signal (HTTP status code, process exit code, …). **Adapter signals are advisory and lossy: a consumer
MUST read the document (the `workflow` field; the participant outcome), never infer the outcome from the
adapter signal.** HTTP 200 can legitimately carry `{"workflow":"failed"}`; a 200 from a participant is the
*binding* of "result-produced," not the meaning itself.

New semantics are added to the neutral model first; each adapter then gets an explicit, advisory mapping.
The status/exit tables in this document (e.g. §4 and §5.1) are **HTTP-adapter bindings**, not the source of
truth. The runner already embodies the seam: the outcome document is rendered by `_oc_render`
(runner.drift:151), the CLI exit code is a separate map (runner.drift:179), and the participant-outcome
vocabulary is the neutral `DispatchResult`/`GetOutcome` variants (runner.drift:1944) that
`_classify_dispatch`/`_get_op` derive *from* HTTP status.

## 1. The two roles

```
client ──HTTP──▶ uflowsd (coordinator) ──HTTP──▶ participant service(s) ──▶ result
                  │                                │
                  └─ Microflows coordinator DB     └─ participant's own durable store
                     (deployment.db, MariaDB)         (e.g. Singular) — SEPARATE domain
```

- **uflowsd** is the long-running coordinator: it loads a *deployment manifest*, compiles each `.mf` script
  to a plan, and drives workflows — dispatching each operation to the owning participant over HTTP, with
  durable idempotent recovery.
- A **participant** is your service. It exposes two routes (PUT/GET an operation) and owns its **own**
  durable store. uflowsd and the participant share **nothing but HTTP** — independent idempotency domains.

---

## 2. Deployment manifest

A single JSON file: top-level **`deployment`** (the shared routing + storage boundary) plus **`scripts[]`**
(the named workflows). Parsed/validated at startup; a bad manifest fails fast — the service refuses to start.
`scripts[].path` is resolved relative to the manifest file. (`RUN_LOCAL.md` §1; loader `runner.drift:310`.)

```jsonc
{
  "deployment": {
    "worker_id": "uflowsd-1",

    "db": {                          // Microflows COORDINATOR schema (not your participant's store)
      "backend": "mariadb",
      "host": "...", "port": 3306,
      "user": "microflows", "password": "...",
      "database": "microflows",
      "connect_timeout_ms": 3000, "io_timeout_ms": 3000,
      "pool": { "keepalive_interval_ms": 30000 }
    },

    "participants": [                // logical id -> transport. The LASTING deployment boundary.
      { "id": "bookkeeper",
        "transport": { "kind": "http",
                       "endpoints": ["https://bookkeeper.svc.internal:8443"],
                       "selection": "ordered_failover" },
        "auth_profile": null }
    ],

    "operations": [                  // op name -> participant + schema_version (+ optional compensation)
      { "name": "microflow-proto-check", "participant": "bookkeeper", "schema_version": 1 }
    ]
  },

  "scripts": [
    { "name": "proto_check", "version": "1.0.0", "path": "workflows/proto_check.mf" }
  ]
}
```

### 2.1 Validation rules (enforced at load)

- **`operations[].name`** — non-empty, **URI-safe segment** (`A-Za-z0-9._-`), **unique**, must reference a
  declared `participants[].id`, `schema_version ≥ 1`. (`_validate_operations`, `runner.drift:2549`,
  `_is_op_name_safe` `:2562`.) Hyphens are allowed — `microflow-proto-check` is valid.
- **Compensation / reversibility** — if any `operations[]` entry declares
  `"compensation": { "operation": "<name>", "schema_version": N }`, that target must itself be a registered
  operation. **Every non-final operation in a workflow graph must be compensable**, or the script is rejected
  at load (`runner.drift:1356`). A terminal (last) operation needs no compensation.
- **`transport`** — `kind` must be `"http"`; `selection` must be `"ordered_failover"`; `endpoints` a
  non-empty array of `http(s)://` URL strings with **exactly one** entry (multi-endpoint failover is not yet
  implemented; `>1` is rejected). (`_validate_transport`, `runner.drift:2676-2705`.)
- **`auth_profile`** — only `null`/absent is supported; any non-null value is rejected
  (`runner.drift:2712`). The API is internal-only; see `RUN_LOCAL.md` "Security boundary".
- **`db`** — points at the **Microflows coordinator** schema (MariaDB), independent of any participant store.

### 2.2 `operations[]` is shared, not per-script

The graph config (`participants`, `operations`, `db`) lives under `deployment` and is **shared across all
`scripts[]`**. Even a one-operation demo registers its operation in `deployment.operations[]`. `plan` is
**never** authored in the manifest — it is compiled from the `.mf` source (§3).

---

## 3. `.mf` scripts (authoring workflows)

Each `scripts[]` entry names a `.mf` source file. At startup uflowsd **lowers (compiles) and validates every
declared `.mf`** over the shared deployment, builds its plan, and type/contract-checks it. App teams author
**`.mf` + manifest** — not a plan.

**Comments:** `.mf` uses `#` to end-of-line **only** — there is no `//` form. A `//` is not skipped and
surfaces as a misleading `expected an identifier` parse error at the `/`. (Lexer: `parser.drift:226`.)

Operation naming, which matters for the wire path:

- `.mf` identifiers are `[A-Za-z_][A-Za-z0-9_-]*` — letters/`_` to start, then letters/digits/`_`/`-`.
  So `op microflow-proto-check { … }` parses (`parser.drift:234-241`).
- **Three-way identity (must match exactly):**

  ```
  .mf:        op microflow-proto-check { input: {…} result: {…} }
  manifest:   "operations": [ { "name": "microflow-proto-check", "participant": "bookkeeper", … } ]
  wire path:  PUT /microflows/v1/operations/microflow-proto-check/{operation_id}
  ```

  The `.mf` op identifier, the `deployment.operations[].name`, and the `{operation}` URL segment are the
  **same literal string**, inserted verbatim (`_op_url`, `runner.drift:1957`). `-` is URL-unreserved, so it
  is wire-safe.

Minimal one-op script:

```
# proto_check.mf
args { payload: string }
op microflow-proto-check { input: { payload: string } result: { state: string } }
steps {
  microflow-proto-check { payload: arg payload }
}
```

Full `.mf` grammar (args, `let`, `if`/`case`/`merge`, `map`/`filter`/`fold` loops, object/array construction)
is in `microflows_design.md §12.6` and `microflows_user_guide.md`. Inspect a compiled plan without a DB via
`microflows-runner --lower-source <file.mf>` (merged config to stdout) or `--parse-check <file.mf>`
(canonical parse outcome). (`runner.drift:308-309`.)

---

## 4. Participant HTTP contract (what you implement)

Your participant exposes exactly two routes over an operation identity uflowsd assigns:

```
PUT /microflows/v1/operations/{operation}/{operation_id}
GET /microflows/v1/operations/{operation}/{operation_id}
```

`{operation}` is the verbatim operation name (§3); `{operation_id}` is a **uflowsd-derived, stable** 32-hex
identity — derived from (workflow instance id + pinned script revision + call site + occurrence), never from
workflow input, identical across retries and recovery. You treat it as an opaque caller-assigned key.

### 4.1 PUT — idempotent create at a caller-assigned id

uflowsd issues a PUT to start an operation. It classifies your response **by status code**
(`_classify_dispatch`, `runner.drift:1985-1997`):

| Your status | uflowsd interprets as | Required body |
|---|---|---|
| **200** | terminal success — settles the operation | `{"state":"succeeded","result":{…}}` — **`result` is mandatory** (uflowsd extracts it; a 200 without `result` is a hard error, `runner.drift:2459`). `state` is not read on 200. |
| **202** | accepted / in-progress — uflowsd defers and re-polls | `{"state":"pending"}` (body not parsed) |
| **400** | definite rejection — abort, no retry | `{"state":"error","reason":"…"}` (reason informational) |
| **409** | identity/input conflict — abort, no retry | `{"reason":"input-conflict"}` (see §4.3) |
| 5xx / unreachable / unreadable body | **uncertain** — uflowsd reconciles by GET (§4.2) | — |

A PUT must be **idempotent under the same `operation_id`**: a re-PUT of the *same* input must NOT execute the
work twice — replay the stored terminal result (200) or report in-progress (202). uflowsd's re-PUTs are
always **byte-identical** to the original (§4.4).

### 4.2 GET — durable status/result lookup

uflowsd GETs to poll in-flight work and to reconcile after an uncertain PUT. Classified by status only
(`_get_op`, `runner.drift:2008-2012`):

| Your status | uflowsd interprets as | Body |
|---|---|---|
| **200** | terminal — settles | `{"state":"succeeded","result":{…}}` — `result` mandatory |
| **202** | pending — keep polling | `{"state":"pending"}` (not parsed) |
| **404** | **no record** of this operation (§4.4) | not parsed — any body, or none |
| **400 / 409** | definite rejection — abort | informational |
| 5xx / unreachable | transport-uncertain — defer + retry | — |

### 4.3 Input-conflict 409 (same id, different input)

If uflowsd ever PUT the *same* `operation_id` with a *different* input, that is a contract violation and you
should answer **409 `{"reason":"input-conflict"}`** — do not start work, do not replay, leave the record
unchanged. In normal operation uflowsd never does this (§4.4), so 409 is a **defense-in-depth guard**, not a
happy path; implement it, but it should not fire.

**Recommended implementation (Singular-backed participants):** persist a canonical input hash as `item_meta`
at `start()`, and on a re-PUT that finds the operation already exists, read the original back from
**`SingularGateway.history(key)[0].item_meta`** (the earliest, Claimed-at-start entry) and compare. This is
the **Phase 3 contract** — `InspectOutcome` does not carry `item_meta`, but `HistoryEntry` does
(`gateway.drift:159-161`, `history()` `:326`). The reference participant implements exactly this
(`participant-stub/app.drift:289, 330-348, 411-430`). (A native `InputConflict` outcome on `start()` is a
tracked future Singular enhancement — not required for Phase 3.)

### 4.4 Never-seen GET, and re-PUT semantics

- **Never-seen GET → 404.** A GET (or, on recovery, a GET-first reconcile) for an operation you have no record
  of must return **404**. Do **not** return 200 (uflowsd would try to settle a non-existent result) or 202
  (uflowsd would poll forever). The 404 body is ignored — a reason-only `{"reason":"…"}` is fine; the
  reference's `{"state":"unknown"}` is **not** required.
- **404 → re-PUT identical.** On a 404 during reconciliation, uflowsd safely **re-PUTs the identical request**
  under the same id, then GETs again (`_reconcile`, `runner.drift:2034-2056`). A genuine first execution and a
  re-creation are indistinguishable because your PUT is idempotent.
- uflowsd **never** intentionally re-PUTs a *changed* body for one `operation_id` — inputs are pinned per
  (workflow instance, pinned script revision, call site).

### 4.5 Envelope vocabulary (real)

- **Succeeded:** `{"state":"succeeded","result":{…}}` — `result` mandatory, returned on a 200 (PUT or GET).
- **Pending:** `{"state":"pending"}` — returned on a 202; body not parsed by uflowsd.
- **Failed:** `{"state":"failed","error":{…}}` — supported by the reference envelope. Note uflowsd settles a
  200 by extracting **`result`**; signal a business failure per your agreed contract (a 200 with no `result`
  is an error to uflowsd). Confirm the failure-signalling shape with the workflows team before relying on it.
- **Error / conflict:** `{"state":"error","reason":"…"}` (4xx) / `{"reason":"input-conflict"}` (409).

### 4.6 Conformance reference

`microflows/participant-stub/src/app.drift` is the **runnable conformance participant** the integration gate
drives — Singular-backed, implements all of the above (idempotent PUT, history-based 409, 404 on unknown).
Read it as the executable spec.

---

## 5. Running uflowsd

```bash
uflowsd --manifest /path/to/manifest.json --port 8088 --log-level info
```

- `--manifest/-m FILE` (required), `--port/-p` (default **8088**), `--log-level/-l debug|info|error`
  (default **info**). (`runner.drift:3130-3144`.) Logs are JSON, ISO-8601, to stderr.
- Health: `GET /healthz` → `{"status":"ok"}`; `GET /readyz` → 200 ready / 503 draining.
- Lifecycle: `SIGUSR1` staged zero-downtime reload (invalid manifest is rejected, old one keeps serving);
  `SIGTERM` drain (new submissions get 503, in-flight converges, then exit). (`RUN_LOCAL.md` §5–6.)

### 5.1 Client workflow API

```bash
POST /v1/workflows/{id}/submit?script=<name>     # body = instance arguments; {id} = caller-chosen 32-hex
POST /v1/workflows/{id}/resume                    # no body/script — drives by the durable pin
```

The **response body is the authoritative outcome document** — read its `workflow` field. The table below is
the **HTTP-adapter binding** (advisory, per §0): the status is a coarse hint, not the outcome. (`RUN_LOCAL.md` §2.)

| Outcome (document `workflow`) | HTTP adapter (advisory) |
|---|---|
| `completed` / `already_terminal` / `reversed` | 200 |
| `pending` / `deferred` / `pending_restart` | 202 / 503 |
| `refused` (draining) | 503 |
| `aborted` (bad args / unknown script / malformed body) | 400 |
| `not_found` | 404 |

Resume is idempotent: a completed workflow replays its terminal result from durable state with **no**
participant call; an in-flight one continues.

---

## Appendix A — what design §5 proposes that is NOT built

`microflows_design.md §5` is a proposed superset. Do not implement against it; these parts diverge from
as-built:

| §5 proposes | As-built (this doc) |
|---|---|
| PUT `201/200` for success | **only 200** is success; 201 falls through to reconcile (`runner.drift:1988`) |
| GET `deferred {state,not_before}` and `indeterminate` states | **not built** — GET is classified by status only; 202→pending, no `not_before`, no `indeterminate` path |
| GET 200 `{state, result\|error}` with `state` significant | uflowsd reads **`result`** only on a 200; `state` is not consulted there (`runner.drift:2459`) |
| body state vocabulary `pending\|succeeded\|failed\|indeterminate` | `indeterminate` is not produced or consumed; uncertainty is handled by reconcile/defer, not a wire state |

When §5 is reconciled to as-built, this table should shrink to empty.

---

## Further reading

- `microflows/examples/` + `RUN_LOCAL.md` — runnable starter kit (manifest + five `.mf` workflows).
- `microflows/participant-stub/src/app.drift` — conformance participant (executable spec).
- `microflows_design.md` §12 (as-built runtime/IR/`.mf`), §14 (manifest registry), §15 (service shell).
- `microflows_user_guide.md` — `.mf` authoring guide.
