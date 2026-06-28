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

## B. #2 — PUT/GET 404 + persistent-404  ✅ LANDED (increments 1–4); ⏳ root gate re-running after review fixes (expect 202) on 0.33.63

- **LANDED 2026-06-28** exactly to the contract below (one deviation: `DispatchResult::Route404` is added,
  but the GET-404 signal reuses the existing `GetOutcome::Unknown` rather than a new `RouteUnknown` — same
  conclusive-form coverage). Increments: (1) schema + migration `0002` + the two reconcile-defer SPs +
  SP-test 127/127; (2) host `reconcile_defer`/`checkpoint_reconcile_defer` + typed outcomes; (3) runner
  `Route404` classification + budget handlers + `Outcome::Blocked(direction,reason)` + `_inspect_report`
  blocked render (gated on `STATE_BLOCKED`) + strict `reconcile_budget` validation; (4) stub `route_404`
  fault + integration 195/195 (forward/reverse exhaustion, transient recovery, replay, invalid-budget).
- **APPROVED IMPLEMENTATION CONTRACT (locked 2026-06-27).** PUT/GET 404 stays **retryable** (infra/rollout
  404s are transient; definite-abort would false-abort financial flows). A **durable, fenced, bounded
  reconcile budget** bounds the retry; on exhaustion the workflow durably **blocks** (operator-visible),
  never `failed`, never `reversed`, never silent infinite pending.
- **(G1) Distinct outcome.** New `DispatchResult::Route404` (+ `GetOutcome::RouteUnknown`). ONLY a confirmed
  participant no-record/route-unknown advances the budget. Conclusive forms: `GET 404 → re-PUT 404` AND
  `GET 404 → re-PUT(5xx/transport) → GET 404`. 202 / 5xx / body-read / transport stay `Pending` (unchanged).
- **(Budget homes)** Forward: `tb_mf_operation` (key `(workflow_id, operation_seq)`). Reverse:
  `tb_mf_workflow_checkpoint` (key `(workflow_id, seq)`). New cols on both: `reconcile_attempts int NOT NULL
  DEFAULT 0`, `reconcile_first_seen_at`/`reconcile_last_seen_at datetime(6) NULL`, `reconcile_reason
  varchar(64) NULL`. Resume re-reads the same row → budget never resets.
- **(G4) Exhaustion rule:** `elapsed >= max_elapsed_ms AND attempts >= min_attempts` (wall-time is the only
  real bound; min_attempts is a small floor so one 404 + clock skew can't block). **No max_attempts cap.**
- **(Transition, fenced + atomic, one SP per direction)** Within budget → advance cols + clear lease +
  set next_attempt + per-attempt warn event `participant_route_404` → `Deferred`. Exhausted:
  - **forward** → `forward(1)→blocked_resolution(3)`, dir forward(1), **disposition indeterminate(4)**, lease
    cleared, event `participant_route_unknown`; **op stays `requested`, NO compensation, prior checkpoints
    untouched**.
  - **reverse** → the EXISTING `sp_mf_checkpoint_reverse_block` path (already takes disposition∈{2,4}; 4 =
    "indeterminate after reconcile exhaustion") → `blocked_resolution(3)` dir reverse, checkpoint
    `resolution_required(3)`.
- **(G2 + G3) Durable blocked reason = the `continuation`** (already returned by inspect). Forward exhaustion
  sets `{"pos":"blocked","direction":"forward","reason":"participant_route_unknown","operation_seq":N,
  "operation_id":"<hex>"}`; reverse sets `{"pos":"blocked","seq":N,"direction":"reverse","reason":
  "participant_route_unknown"}` (preserve checkpoint `seq` — the existing reverse-block continuation uses it).
  `_inspect_report`'s non-terminal branch parses `pos=="blocked"` → `Outcome::Blocked(direction, reason)`,
  with a **fallback**: `direction` ← continuation else `snap.execution_direction`; `reason` ← continuation
  else `""`. So NEW blocks replay with full reason; pre-existing reverse-block rows (which carry
  `{pos:blocked,seq}` only) replay as `blocked`/reverse with an empty reason — **no migration of old
  continuations is claimed.**
- **Config:** `deployment.reconcile_budget = { max_elapsed_ms, min_attempts }`. Default production-generous
  (`1_800_000` ≈ 30 min, `2`); gate/dev override short. One knob, forward + reverse.
- **Render:** parameterize `Outcome::Blocked(direction, reason)` → `{"workflow":"blocked","direction":
  forward|reverse,"reason":…}`, exit 3 (unchanged).
- **Operator resolution/reset is a NAMED FOLLOW-UP — NOT shipped here.** Docs say "persistent route-404 →
  operator-visible block; the resolution/reset API is a follow-up." Do NOT write "fix routing then resume."
- **(G4-stub) Tests need a real route-404 fault:** a stub hook returning 404 on PUT *and* GET **without
  creating a Singular op** (no record), in persistent + transient (N-then-recover) forms. Transient proves
  the budget increments then route recovery completes with NO compensation.
- **Build order (incremental, SP tests before integration):** (1) schema + migration `0002` + the two budget
  SPs (forward reconcile-defer; reverse reconcile-defer→reverse_block) + sp_operation_test units; (2) host
  decode (`Route404`/budget outcomes, `Outcome::Blocked` direction); (3) runner routing (`Route404`
  classification in `_reconcile`/`_reconcile_after_resubmit`) + `_inspect_report` blocked render; (4) stub
  route-404 fault + integration tests + docs.
- **Future (separate, not a blocker):** a non-mutating participant capability endpoint for deploy-time
  preflight (collection-level `GET /microflows/v1/operations/{operation}` preferred over `OPTIONS` for
  infra-predictability). Preflight is impossible on the current 2-route contract (GET 404 ambiguous, PUT
  mutates), so runtime bounded-retry is the real fix.
- **Code:** `_classify_dispatch` (runner.drift:1985) / `_reconcile` (:2034) keep 404 retryable but bounded;
  durable budget read/write; expiry → new terminal mapping.

## C. #3 — `200` = result-only  ✅ (items 1–2 clear-cut; item 3 behind #4)

- **Decision:** `200` ⇒ `result` **mandatory**; `state` advisory (coordinator consumes status + `result` only
  — `_classify_result` runner.drift:2057). Business outcomes (approved/declined/indeterminate) live in
  `result`. Non-200: `400`=app rejection, `409`=input conflict, `202`=pending, `404`/`5xx`/transport=infra →
  reconcile/bounded-block.
  1. **Doc** → `200` result-only (partly landed via §0/§4; finalize §4.5 envelope wording, remove the
     `state:failed` bullet). **CLEAR-CUT.**
  2. **Reference stub** → `participant-stub/app.drift` `_envelope_terminal` Failed arm (:824) stops rendering
     `200 {state:failed}`; map to a `result` object so 200 stays result-only. Safe: the stub never produces a
     Failed terminal (only `complete()`s with success); no test depends on it. **CLEAR-CUT.**
  3. **Runner harden — LANDED (Step 6).** `200`-without-`result` is now a non-throwing definite rejection
     (`participant_protocol_missing_result`) via `_classify_200_body` in the PUT + reconcile-PUT paths and a
     `GetOutcome::Rejected` in the GET path; it flows through the Step-4 terminal `failed` surface (exit 3,
     compensated per the prior stack). The throwing `_extract_result` was removed.

## D. #3 decline-unwind + authored `fail` (the big feature)  ✅ shape

- **Decision:** a business decline is a `200` result; the **workflow** decides whether to unwind, not the
  participant (a participant can't own workflow-policy without breaking reuse across workflows). Add `.mf`
  author-driven failure.
- **Prerequisite = the real lift:** **result-conditional branching** — **LANDED (Step 5).** `if`/`case` now
  select on a path selector (arg/result/local); `case result auth.status { … }` parses. (Was: arg-paths only.)
  Touched parser + IR validation (the IR/drive already evaluated result/local). `fail` is
  the easy leaf on top.
- **`fail <string-reason>`:** terminal graph node in an `if`/`case` branch (LANDED as a String reason code, not an object); durably records the reason and begins
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
- **Code (LANDED — see Sequencing/step 3):** `_run_reversal` (assemble from durable state + strict-parse the
  forward input/result, distinct `reverse_forward_{input,result}_invalid` defers, never synthesize);
  `_assert_compensation_types` **no-op'd** (structural/opaque v1 — comp input type no longer cross-linked to
  the forward; types still fold into `content_hash`); participant compensation handlers migrate by
  `schema_version`.

## F. #4 — `failed`/`compensated` terminal  ✅ (bundles with D)

- **Decision:** drop `reversed` as a top-level success-shaped value; collapse to **`failed`** + **`compensated`**
  bool + **`reason`**. `blocked` = stuck mid-unwind (a compensation itself failed) → operator.
  - `{"workflow":"failed","reason":…,"compensated":true}` — aborted, rollback ran
  - `{"workflow":"failed","reason":…,"compensated":false}` — aborted, nothing to roll back (first-op / empty)
- 🔧 **Verified (pre-step-4 finding, now RESOLVED).** The old single durable terminal `STATE_REVERSED=5`
  collapsed empty vs real unwinds, so a resume re-rendered `reversed`. Step 4 split them: empty →
  `BeginReversalOutcome::Failed` → durable `STATE_FAILED(7)`; real unwind stays `STATE_REVERSED(5)`; both
  carry a durable `terminal_reason` so replay renders deterministically ⇒ **#4 bundled
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

## H. User-facing doc update (lands WITH its implementing code, not ahead)

Each doc section becomes true only when its code lands; gate accordingly. Content to cover:
- `200 {"result":…}` = a **valid business result**, not necessarily business *success* (ships step 1).
- Business policy lives in `.mf`: branch on results, then `fail` to trigger compensation (with D).
- `fail` unwinds **all** settled compensable checkpoints, including the op whose result caused it (with D).
- Compensation input is always the standard forward-context envelope `{forward:{workflow_id, operation,
  operation_id, schema_version, input, result}}` (with E).
- Compensation must be idempotent + safe-no-op when the forward result says no external effect happened (with E).
- `forward.operation_id` = correlation; the compensation request **URL** operation_id = the compensation's
  idempotency key (with E).
- Transport-neutral terminal outcomes: `{"workflow":"completed","result":…}` /
  `{"workflow":"failed","reason":…,"compensated":true|false}` / `{"workflow":"blocked","reason":…}`;
  **`reversed` is not a client-facing success status** (with F).
- Dynamic side-effect fan-out is unsupported inside loops; one bulk operation now, child workflows later (G).

Targets: `microflows/doc/uflowsd_participant_contract.md` (wire contract), `microflows/doc/microflows_user_guide.md`
(author examples), `microflows/examples/` (a concrete **payment-decline + compensation** example).

## Sequencing & dependencies

- **STEP 1 LANDED + gate-green (165/165), uncommitted on `76032d4`:** A (`.mf` comments `#`→`//`+`/* */`:
  lexer + 8 `.mf` migrated + 4 new fixtures, 81/81; inline `#` fixtures in test.py×2 + parser_test.drift×2
  migrated — caught by the gate) and C items 1–2 (`200` result-only: stub Failed arm + contract doc §4.5).
  Build note: certified deps now live under `~/opt/drift/certified/current/pkgs` (not `libs`).
- **Landed (doc-only):** contract doc (`uflowsd_participant_contract.md`, incl. §0 + comment note + §4.5
  result-only + §5.1 table reframe), design `§5` superseded banner, `RUN_LOCAL.md` see-also pointer.
- **STEP 2 LANDED + gate-green (165/165):** node-address forward operation IDs (G) — `_operation_id_node`
  (domain-tagged, length-prefixed, "node ≤ once" invariant documented); pinned `content_hash` threaded into
  `_run_forward`; fresh dispatch + prior-settled use node-address, terminal-replay reads the stored id, legacy
  single-op kept seq-based (forward-only). Reverse id deferred to step 3 (rides the reversal/envelope edits).
- **STEP 3 LANDED + gate-green (165/165):** compensation forward-context envelope (E). Runner: `_run_reversal`
  Pending arm assembles `{forward:{workflow_id,operation,operation_id,schema_version,input,result}}` from
  durable state (`operation_request_get` for id/sv, `operation_result` for result; missing → defer
  `reverse_forward_request_missing`/`_result_missing`); `_comp_envelope` helper; `_assert_compensation_types`
  no-op'd (structural/opaque v1, comp type no longer cross-linked to forward). Stub: `_effective_input` unwraps
  the envelope (forward.input for comps, body for forward ops) — all validate/execute/fault reads use it; the
  409 item-meta hash still covers the full body. Tests: repurposed `typed_compensation_input_type_not_cross_checked`,
  fixed 2 `reverse_input_json` JSON paths → `$.forward.input.reservation`, added 13 forward `tb_mf_operation`
  seed rows for the reverse fixtures (checkpoints lacked them). Docs: contract §4.6/§4.7, user guide §9.
  Reverse id kept seq-based (out of scope). NOTE: the microflows COMPONENT e2e (`live_reversal_test.drift`,
  proc-seeded) is a separate gate — verify it if it drives reversal through the runner.
- **STEP 4 LANDED + gate-green (166/166):** `failed`/`compensated` durable terminal (F). `state.drift`
  `Failed`(7); schema `terminal_reason`+CHECKs+`migrations/0001`; SPs (`begin_reversal` empty→FAILED+reason /
  non-empty stores / failed+already_begun return reason, `reverse_settle` returns reason, `inspect` terminal+7
  +reason); host (`BeginReversalOutcome::Failed`+`AlreadyBegun` reason, `ReverseSettleOutcome::Reversed(reason)`,
  `WorkflowSnapshot.terminal_reason`); runner `Outcome::TerminalFailure(reason,compensated)` (state 5→true,
  7→false; exit 3; HTTP 200; replay from durable `terminal_reason`). ~20 reverse tests flipped `reversed`→
  `failed`+`compensated`; +`forward_first_reject_reverses_replay`; client-facing `reversed` removed from docs.
  COMPONENT gate also updated (`state_test.drift` state-7 coverage, `live_reversal_test.drift` new host
  variants, `sp_operation_test.py` no-checkpoint→failed/7+terminal_reason). **ROOT `just test` GREEN**:
  singular 16, microflows (parser 81/81, e2e 20, SP 110/110), integration 166/166.
- **STEP 5 LANDED + root-gate-green** (singular 16, microflows parser 90/90 / e2e 20 / SP 110, integration
  172/172). Result-conditional branching + authored `fail`:
  - **Selectors (parser-only):** `if`/`case` take a path selector `arg`/`result`/`local` (bare = arg); IR
    `EResult`/`ELocal` + `advance` already evaluate them, `_check_expr` dominance already enforces
    prior-dominating OK / branch-local-without-merge REJECTED / merge-carried OK.
  - **`fail <string>`:** `ir.NFail` + `StepOutcome::Fail`; reason is a String code (non-string/>190 rejected
    at type-check/lower for literals; a dynamic non-string/overlong reason UNWINDS with the durable code
    `invalid_fail_reason` — never stranding checkpointed side effects). New SP
    `sp_mf_workflow_authored_fail` (own transition, NOT begin_reversal — follows a settled 200); fail command
    id `H(workflow_id, content_hash, "fail:"+node_id)` = durable trigger (replay-safe; mismatch ->
    trigger_mismatch). Reuses Step-4 terminal rendering (empty->compensated:false, unwind->true, blocked->blocked).
  - **Finality fix:** runner PROBES `advance` with the settled result; `operation_settle` finality =
    `seq==plan_length AND caller is_final` (downgrade-only) -> an op at plan_length whose result branches to
    `fail` is checkpointed, not completed. `op_depth`/`nonfinal_operations`/`validate_graph` made fail-aware.
  - **Example + docs:** `payment_decline_guard.mf` + manifest; user-guide selectors/`fail`; RUN_LOCAL 4b.
- **STEP 6 LANDED + gate-green** (last of the bundle): a participant `200` with a MISSING or NON-OBJECT
  `result` is a non-throwing definite rejection — `participant_protocol_missing_result` /
  `participant_protocol_invalid_result` — via `_classify_result` (object/invalid/missing) in `_classify_200_body`
  (PUT + reconcile-PUT) and `GetOutcome::Rejected` (GET) → Step-4 terminal `failed` (exit 3; compensated per
  the prior stack). The throwing `_extract_result`/`_try_extract_result` are gone (a non-object result would
  otherwise reach Done, then throw in operation_settle = runner-fatal). Tests: first-op→compensated:false (replay
  stable), later-op→compensated:true (prior checkpoint unwound), GET-reconcile (lost-ack)→same, valid 200
  still completes. **The `.mf` comment switch + #3–#5 + this 200-harden are landed; #2's decision/design is
  landed too, but its durable bounded reconcile budget (§B) remains the one open fork — not yet built.**
- **Clear-cut, ready on greenlight:** C items 1–2 (doc + reference stub); G once direction is picked.
- **One unit (keystone):** F (`failed` terminal) + D (authored-fail) share the durable reason/state — implement
  together; C item 3 (runner harden) lands behind them.
- **Open fork:** B's (#2) durable reconcile budget (schema or not) is the one real decision left. D's
  result-branching scope is now DECIDED + LANDED (Step 5).

## Verified-in-code facts (so they aren't re-litigated)

- Hyphenated op idents work everywhere; comment lexer is C-family `//` + `/* */` (no `#`) after step 1 (parser.drift:221, 234-248).
- Coordinator consumes status + `result`; `state` read nowhere (`_classify_result` runner.drift:2057; missing/non-object result -> `_classify_200_body`:2074).
- `if`/`case` select on a path selector — arg/result/local (LANDED Step 5; was arg-paths only).
- Reversal can reach forward input (`operation_request_get`/`reverse_head`) AND result
  (`operation_result(seq)` host.drift:471) — envelope is wiring, no schema migration.
- (Pre-#4 finding, RESOLVED in Step 4) the old single terminal `STATE_REVERSED=5` made resume re-render
  `reversed`; #4 split it into `reversed(5)`=unwound (compensated:true) vs `failed(7)`=no-unwind
  (compensated:false), both carrying a durable `terminal_reason`.
