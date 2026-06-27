# uflowsd / microflows — next-release design plan (#2–#5 + `.mf` comments)

Converged via design review with pushcoin (this thread). Target: the **next microflows release** after
certified driftc 0.33.61 / `uflowsd 0.1.0` (`main@76032d4`). All file:line refs are against that tree.

**Status legend:** ✅ decided · 🔧 decided + verified in code · 🟡 open fork · ⏳ awaiting user.

---

## 0. Governing constraint — transport neutrality (LANDED, doc-only)

Canonical contracts are two **transport-neutral documents**: the **workflow-outcome document**
(`workflow`/`reason`/`compensated`/`result`) and the **participant-outcome vocabulary**
(`result-produced | pending | rejected | conflict | no-record | uncertain`). HTTP status + CLI exit code are
**advisory, lossy adapter bindings** — consumers read the document, never infer from the adapter. New
semantics go in the neutral model first. Already written into `microflows/doc/uflowsd_participant_contract.md`
§0; the runner already embodies the seam (`_oc_render` runner.drift:151; exit map :179; neutral
`DispatchResult`/`GetOutcome` :1944 derived *from* HTTP by `_classify_dispatch`/`_get_op`).

## Keystone primitive (shared by #2, #3, #4, authored-fail)

A first-class **`failed` terminal that does NOT unwind**, distinct from `reversed`/compensated. The Outcome
model has `Aborted` (non-unwinding), but a definite forward rejection *always* routes to reversal today — that
single fact is the root of the #4 wart and is what #2/#3/#4/authored-fail all need fixed. Decide #4's terminal
first; the rest inherit it.

---

## A. `.mf` comments: `#` → `//` + `/* */`  ✅

- **Decision:** drop `#`; support `//` line + `/* */` block (non-nesting, C-style). Pre-1.0, one C-family
  convention. Zero grammar ambiguity — `/` appears nowhere in the grammar; comments are stripped before
  `content_hash`, so goldens are unaffected.
- **Verified:** hyphenated op identifiers already work end-to-end (`--parse-check`, `--lower-source`, and a
  real `uflowsd --manifest` logged `manifest-load-completed` on `op microflow-proto-check` + script
  `proto-demo`). The pushcoin "blocker" was a `//` comment, **not** dashes: `//` isn't skipped, so the parser
  hits `/` and reports the misleading `expected an identifier` (only `#` is skipped — `_skip_ws`
  parser.drift:226).
- **Code:** `_skip_ws` (parser.drift:221) — remove byte-35 branch, add `//`-to-EOL + `/* */`; unterminated
  `/*` → record on the Cursor (new field, init −1) and surface a precise error at `parse_source`/`lower`
  entries (keeps `_skip_ws` `nothrow` — 18 callers incl. 2 nothrow lookaheads). Migrate the 8 `.mf` files
  using `#` (5 `examples/workflows/*.mf` + 3 fixtures: `check/plan_refund_straightline.mf`,
  `check/graph_named_op_result_renamed.mf`, `lower/lower_overlay.mf` — `#` only on line-1 descriptions →
  goldens unaffected). Add fixtures for `//`, `/* */`, unterminated `/*`, and `#`-now-error; regen via
  `tests/run_parser_fixtures.py --update`. Update the contract-doc comment note (currently says `#`-only).

## B. #2 — PUT/GET 404 + persistent-404  ✅ / 🟡 (schema fork)

- **Decision:** PUT 404 is **not** a definite rejection (infra LB/mesh/ingress/rollout 404s are transient;
  definite-abort would false-abort financial flows). Keep PUT/GET 404 **retryable**. `GET 404 = no durable
  record`. Add a **durable, bounded reconcile budget** (count **+** elapsed wall-time; wall-time primary with
  a generous min-elapsed floor to outlast deploy windows; **configurable in the deployment manifest**).
  Per-attempt structured warn event `participant-route-404` (op, operation_id, participant, endpoint, attempt).
  On budget expiry → non-success terminal **`blocked`** (resumable, operator) / `failed` with
  `participant_route_unknown` — never silent infinite pending, never `reversed`. Counter increment is
  fence-guarded.
- 🟡 **Open fork:** the budget must be **durable** (survive resume/restart, else it resets and the spin
  survives) → likely a coordinator-schema field (operation/checkpoint row or a reconcile-state record).
  *This is the decision that makes #2 runner-only vs schema+runner.*
- **Future (separate, not a blocker):** a non-mutating participant capability endpoint for deploy-time
  preflight (collection-level `GET /microflows/v1/operations/{operation}` preferred over `OPTIONS` for
  infra-predictability). Preflight is impossible on the current 2-route contract (GET 404 ambiguous, PUT
  mutates), so runtime bounded-retry is the real fix.
- **Code:** `_classify_dispatch` (runner.drift:1985) / `_reconcile` (:2034) keep 404 retryable but bounded;
  durable budget read/write; expiry → new terminal mapping.

## C. #3 — `200` = result-only  ✅ (items 1–2 clear-cut; item 3 behind #4)

- **Decision:** `200` ⇒ `result` **mandatory**; `state` advisory (coordinator consumes status + `result` only
  — `_try_extract_result` runner.drift:2459). Business outcomes (approved/declined/indeterminate) live in
  `result`. Non-200: `400`=app rejection, `409`=input conflict, `202`=pending, `404`/`5xx`/transport=infra →
  reconcile/bounded-block.
  1. **Doc** → `200` result-only (partly landed via §0/§4; finalize §4.5 envelope wording, remove the
     `state:failed` bullet). **CLEAR-CUT.**
  2. **Reference stub** → `participant-stub/app.drift` `_envelope_terminal` Failed arm (:824) stops rendering
     `200 {state:failed}`; map to a `result` object so 200 stays result-only. Safe: the stub never produces a
     Failed terminal (only `complete()`s with success); no test depends on it. **CLEAR-CUT.**
  3. **Runner harden** → `200`-without-`result` becomes a clear rejection (`participant_protocol_missing_result`),
     not a thrown runner fatal. Sites: `_classify_dispatch`:1988, `_reconcile`:2047 (PUT), `_get_op`:2012 (GET,
     currently → `TransportFailed`). Remove the now-unused throwing `_extract_result` (:2451). **HOLD behind
     #4** so it lands as `failed`, not `reversed`.

## D. #3 decline-unwind + authored `fail` (the big feature)  ✅ shape

- **Decision:** a business decline is a `200` result; the **workflow** decides whether to unwind, not the
  participant (a participant can't own workflow-policy without breaking reuse across workflows). Add `.mf`
  author-driven failure.
- **Prerequisite = the real lift:** **result-conditional branching.** Today `if`/`case` select on **arg-paths
  only** (parser grammar header parser.drift:24-33); `case result auth.status { … }` does not parse. Extend
  `if`/`case` selectors to `result`/`local` paths — touches parser + IR + validation + drive-loop. `fail` is
  the easy leaf on top.
- **`fail <object-expr>`:** terminal graph node in an `if`/`case` branch; durably records the reason and begins
  reversal over the **full** settled-compensable checkpoint stack. Empty stack → clean `failed` (no unwind);
  non-empty → compensated.
- **Option A (decided):** `fail` unwinds **all** settled compensable checkpoints **including** the op whose
  result triggered it. Coordinator stays domain-free (unwind the stack); **new requirement: compensations must
  be idempotent / safe-no-op on business-negative results** (voiding a declined auth = no-op).
- **Code:** parser grammar (`fail` + result selectors), `ir.drift` (node kinds + hashing + validation),
  lowering, runner drive (`fail` node → `begin_reversal` with reason), Outcome rendering.

## E. Compensation forward-context envelope  🔧 (verified wiring)

- **Decision:** compensation request body becomes a standard envelope (was forward-**input** only):
  ```json
  {"forward":{"workflow_id":"…","operation":"authorize-payment","operation_id":"…",
              "schema_version":1,"input":{…},"result":{…}}}
  ```
  Fixes a **latent flaw**, not just declines: compensations need result-produced ids (auth_id, reservation_id,
  capture_id) they cannot see today (stub fakes it — `participant-stub/app.drift` `_biz_spec` derives from
  input only).
- **Typing:** structural/opaque v1 — coordinator validates envelope **shape**, not the semantic types of
  `forward.input`/`forward.result`. Breaking compensation contract → gate on compensation `schema_version`.
  Forward ops stay input-only. `content_hash` bumps for compensation-bearing workflows.
- 🔧 **Verified: WIRING, no schema migration.** In `_run_reversal` (`ReverseHeadOutcome::Pending(seq, op,
  payload)` arm) assemble the envelope from durable reads: `forward.input`/`operation`/`schema_version` from
  **`operation_request_get(seq).Found(rec)`** (use `rec.schema_version` — the *settled* invocation, not a
  registry lookup that can drift after deploy; forward drive already reads it at runner.drift:691);
  `forward.result` from **`operation_result(workflow_id, seq).Succeeded`** (host.drift:471, durable, the same
  read data-flow uses). `reverse_head` just drives the loop; `reverse_request` persists the envelope →
  replay-safe.
- **Identity:** the **reverse operation_id (URL) is the idempotency/identity key**; the envelope input-hash is
  **only** input-conflict (409) detection, not identity. `forward.operation_id` is **correlation**.
- **Inconsistency:** `operation_result` not `Succeeded` for a reverse checkpoint → defer/block/abort **loudly**;
  never synthesize an envelope.
- **Code:** `_run_reversal` (assemble + the extra `operation_result` read); `_assert_compensation_types`
  (runner.drift:1586 — flip from "comp input == forward input" to "comp input == envelope"); participant
  compensation handlers migrate by `schema_version`.

## F. #4 — `failed`/`compensated` terminal  ✅ (bundles with D)

- **Decision:** drop `reversed` as a top-level success-shaped value; collapse to **`failed`** + **`compensated`**
  bool + **`reason`**. `blocked` = stuck mid-unwind (a compensation itself failed) → operator.
  - `{"workflow":"failed","reason":…,"compensated":true}` — aborted, rollback ran
  - `{"workflow":"failed","reason":…,"compensated":false}` — aborted, nothing to roll back (first-op / empty)
- 🔧 **Verified: not renderer-only.** Single durable terminal `STATE_REVERSED=5` (runner.drift:226); no
  `STATE_FAILED`. The live drive distinguishes empty (`BeginReversalOutcome::Reversed`) vs has-work
  (`Reversing`), but durable state collapses them, so a **resume re-renders `reversed`** (runner.drift:2113).
  The distinction must be durable → carried by the **same durable reason authored-fail adds** ⇒ **#4 bundles
  with D**, not a convenience.
- **Minimal storage:** new **`STATE_FAILED`** (→ `failed, compensated:false`) vs `STATE_REVERSED`
  (→ `failed, compensated:true`); both carry the durable `reason`. The `compensated` flag rides the state code.
- **"Success-shaped" fix = body value + exit code**, not necessarily HTTP: `{workflow:"failed"}` body +
  **non-zero exit** (runner.drift:152/180). HTTP can stay 200 (it delivered a definitive terminal answer — §0
  transport-neutral); a non-2xx mapping is a defensible separate choice but not required.
- **Code:** `_begin_reversal_unwind` Reversed arm (runner.drift:1904); resume render (:2113); `_oc_render`
  (:152); exit map (:180); outcome→HTTP table (doc).

## G. #5 — operation-id = node-address identity  ✅ (decided 2026-06-27)

- **Finding:** `_operation_id` (runner.drift:2488) = `workflow_canon_hex` + **constant** `SCRIPT_REVISION` +
  `operation_seq` + hardcoded `"inv:1"`. This is positional (execution order), not call-site identity.
- **Decision (move impl to node-address, correct the doc to match):**
  - `operation_id = H(workflow_id, pinned_script_content_hash, operation_node_id)`
  - `reverse_operation_id = H(workflow_id, pinned_script_content_hash, operation_node_id, "reverse")`
  - **Invariant (enforced, not just documented):** *a compiled operation node executes at most once per
    workflow instance.* No occurrence dimension — ever (occurrence would break the 1:1 checkpoint/correlation
    model that reversal + the envelope rest on). Already structurally true (acyclic graph runner.drift:1886 +
    side-effect-free loops parser.drift:501-518); promote it to a **checked assertion at registry/graph
    validation** so a future grammar addition fails loudly instead of minting colliding ids.
  - **Separate identity from ordering:** `seq` remains the internal checkpoint-stack / unwind ordering key;
    `operation_id` (participant-facing) becomes node-address. Stop deriving identity from the ordering key.
- 🔧 **Verified migration-safe (forward-only):** the persisted request carries the id
  (`OperationRequestRecord.operation_id`) and resume **adopts** it (`operation_id = _copy_bytes(&rec.operation_id)`
  on `Found`, runner.drift:687-693). So the new derivation runs only for **fresh** dispatches; in-flight
  workflows keep their persisted ids → no drain, no double-dispatch.
- **Hashing hygiene:** domain-separated + length-prefixed construction (distinct forward/reverse tags, no raw
  concat) so forward/reverse id spaces can't collide (mirror the stub's `_join`); source
  `content_hash`/`node_id` from the pinned/canonical IR (canonical already embeds node ids `G{…n0…}` →
  self-consistent).
- **Known cost to document:** "node ≤ once" + "no ops in loops" ⇒ **no dynamic operation fan-out** (can't call
  a participant once per element of a runtime-sized list). v1 escape hatch: encapsulate the collection inside a
  **single** operation (participant handles it). **v-next recommended pattern (separate milestone, NOT this
  bundle): spawn child workflows** — one per item, `child_workflow_id = H(parent_id, item_key)`. The rule that
  makes it consistent: a **spawn MAY appear in a loop** (identity is item-keyed, idempotent re-spawn), while a
  **participant-op may not** (it would need an occurrence index). Implement the child as a participant-shaped
  endpoint (spawn=PUT / await=GET) to reuse dispatch/reconcile/#2/envelope/outcome machinery. New complexity
  lives in parent/child: cross-workflow compensation (parent's comp for a spawn = reverse the child; correlation
  key = child id — layers on E), the parent as a join over child outcomes (reuses #4/F `failed/compensated/
  blocked`), and a **durable** spawn set (item list from a settled result/arg, stable item keys, else orphaned
  children). Net-new; no spawn construct in the grammar today. Document the limit + both answers in the contract.
- **Code:** `_operation_id` (runner.drift:2488) + `_reverse_id` → node-address; validator assertion; doc.
  Land **early / with E (envelope)** since E uses `forward.operation_id` as correlation. Independent of the
  #2/#3/#4 forks.

---

## Sequencing & dependencies

- **Landed (doc-only, uncommitted on `76032d4`):** contract doc (`uflowsd_participant_contract.md`, incl. §0 +
  comment note + §5.1 table reframe), design `§5` superseded banner, `RUN_LOCAL.md` see-also pointer.
- **Clear-cut, ready on greenlight:** C items 1–2 (doc + reference stub); G once direction is picked.
- **One unit (keystone):** F (`failed` terminal) + D (authored-fail) share the durable reason/state — implement
  together; C item 3 (runner harden) lands behind them.
- **Open forks:** B's durable budget (schema or not) and D's result-branching scope are the two real decisions
  left.

## Verified-in-code facts (so they aren't re-litigated)

- Hyphenated op idents work everywhere; the comment lexer skips `#` only (parser.drift:226, 234-248).
- Coordinator consumes status + `result`; `state` read nowhere (runner.drift:2459).
- `if`/`case` select on arg-paths only today (parser.drift:24-33).
- Reversal can reach forward input (`operation_request_get`/`reverse_head`) AND result
  (`operation_result(seq)` host.drift:471) — envelope is wiring, no schema migration.
- Single terminal `STATE_REVERSED=5`; resume re-renders `reversed` (runner.drift:226, 2113) — #4 needs durable
  state, not just a renderer change.
