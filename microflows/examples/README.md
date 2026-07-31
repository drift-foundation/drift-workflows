# Microflows starter kit — business workflow templates

This is the **shape your first Microflows workflow should take.** Copy these `.mf` files and the
manifest, point them at your participant services, and adapt the payloads. Every template here is a
real, runnable workflow exercised end-to-end by the integration gate (`integration/coordinator-singular`,
checks `ex_*`), not pseudo-code.

> **Security boundary (intentionally deferred).** Every participant here sets `auth_profile: null` and
> the service is internal-only. Production auth / user / session / security context is **not** designed
> yet — by intent. See `RUN_LOCAL.md` → "Security boundary" for the open questions we want the business
> team to answer before we build it.

## What's here

```
examples/
  manifest.json     # the service manifest: 7 named scripts + ONE shared deployment/routing registry
  deployment notes  # the `deployment` block inside manifest.json is the shared routing registry:
                    #   participants (payments / inventory / accounts), operation contracts, and
                    #   compensation bindings. (db + endpoints are placeholders to fill in.)
  workflows/
    payment_authorize_capture.mf       # authorize → capture → record_ledger  (2 compensations)
    payment_refund.mf                  # one idempotent corrective op
    inventory_reserve_release.mf       # reserve_inventory → commit_shipment  (1 compensation)
    account_adjustment_with_rollback.mf# adjust_balance → post_journal        (auto-rollback)
    payment_decline_guard.mf           # authorize → case result status → fail (result-branch + authored fail)
    checkout_branch_merge.mf           # BRANCH + MERGE + COMPENSATION in one flow
    shipment_booking.mf                # a small child workflow: one op (book_shipment) + compensation
    order_fulfillment.mf               # PARENT: calls shipment_booking, then charge_payment
                                        #   (workflow composition + reverse-child compensation)
  RUN_LOCAL.md                         # start the service, submit/resume/reload over HTTP
```

## The shape (what every template teaches)

1. **A workflow is a thin orchestrator.** It calls **typed remote operations** on participant services
   and threads their results forward (`result auth.auth_id`). It does **not** do business arithmetic or
   own data — the participants do. Inputs are assembled inline from arguments/results
   (`authorize { order_id: arg order_id, amount: arg amount }`).

2. **Operation contracts live in two places, on purpose:**
   - the **`.mf`** declares each operation's input/result *shape* (`op authorize { input: {…} result: {…} }`)
     and the business flow;
   - the **deployment registry** (`manifest.json` → `deployment.operations`) binds each operation to a
     **participant** and declares its **compensation** (reverse operation). One deployment, many scripts.

3. **Compensation is automatic and declared, never hand-coded.** If a later step fails, Microflows runs
   the completed steps' declared reverse operations **in reverse order**, leaving a clean state. Every
   **non-final** operation must declare a compensation (so a partial workflow is always reversible); the
   final step needs none. Reverse operations receive the **forward checkpoint payload**.

4. **Durable + idempotent by construction.** Re-submitting the same workflow id reconciles to the same
   result (operations are looked up by a stable id), so retries and recovery never double-charge or
   double-ship. Pending participants are resumed; terminal workflows replay their result from durable
   state without touching the participant.

5. **Branch + merge for durable decisions.** `case region { … } merge reservation = …` picks a path
   from a **durable argument** and joins the arms into one binding the tail reads — see
   `checkout_branch_merge.mf`.

6. **Workflow composition** (`order_fulfillment.mf` calling `shipment_booking.mf`) — a step can
   `call` another workflow and await its result, exactly like a participant operation:
   - **`call` is async but *awaited*** — `let shipment = call shipment_booking@1.0.0 { order_id: arg
     order_id, sku: arg sku, quantity: arg quantity }` occupies one durable step and only advances
     once the child reaches a terminal state; `result shipment.shipment_id` reads its typed return
     downstream, same as a participant result.
   - **The parent compensates the call as ONE checkpoint.** If a later step (here, `charge_payment`)
     fails, the parent's reversal reaches the call checkpoint and treats "undo this child" as a
     single unit — it never reaches into the child's own steps.
   - **The child owns its own internal compensation.** The parent just asks the (already-completed)
     child to reverse itself; `shipment_booking.mf` unwinds its *own* `book_shipment` step via
     `cancel_shipment` exactly like any other reversal, with no awareness that a parent asked it to.
   - **A blocked/non-terminal child never blocks the parent.** Whether waiting on a child that hasn't
     finished yet (forward) or one that hasn't finished compensating yet (reverse), the parent simply
     stays `pending` — it never adopts the child's own stuck state.
   - **Not in this MVP** (see `microflows_design.md` §16 for the full design/status): **fan-out** (one
     `call` is one child, no "call N children and gather"), **`on failed`/failure-as-data** (a child
     that terminates *without completing* — rejected, reversed, or failed — always drives the
     parent's *own* reversal; a non-terminal/blocked child never does, it just keeps the parent
     `pending` per the point above — there is no way for the parent's script to branch on either
     outcome as a value), and **a separate compensating-workflow mode** (`compensation
     <wf>@<version>` stays build-rejected; a child
     compensates via its own ordinary reversal, not a distinct authored "compensation script").

## Adapting these to your services

- Replace the operation **names**, **payloads**, and **result fields** with your participants' real
  contracts (the templates use realistic-but-illustrative ones: `authorize → {auth_id}`, etc.).
- Fill in `deployment.db` and each participant's `endpoints` in `manifest.json`.
- Keep every non-final operation's `compensation` accurate — that is what makes a half-done workflow
  safely reversible.
- Leave `auth_profile: null` until the security model is designed (see the boundary note above).

## Proven behavior (integration `ex_*`, real HTTP + Singular-backed participant)

| Template | Proven |
|---|---|
| payment_authorize_capture | completes; deterministic ledger entry |
| account_adjustment_with_rollback | later-step failure → **automatic compensation** (workflow `failed`, `compensated:true`) |
| payment_decline_guard | **result-conditional** branch on the gateway's 200 decision; a decline calls `fail` → unwinds the authorization → `failed`, `compensated:true` |
| payment_refund | idempotent single-op corrective flow |
| inventory_reserve_release | participant **pending → resume** completes |
| checkout_branch_merge | branch + merge + compensation runs end to end |
| order_fulfillment (calls shipment_booking) | typed call + return completes; later-step failure → **reverse-child compensation** (parent `pending` on the child, no cascade; child compensates itself; parent ends `failed`, `compensated:true`) |
| (any) | manifest **reload** doesn't break a pinned older workflow; **terminal replay** with the participant down |

Toolchain: driftc 0.33.91+ / ABI 22. As-built design: `microflows/doc/microflows_design.md` §15 (service),
§12 (the language/IR), and §16 (workflow composition).
