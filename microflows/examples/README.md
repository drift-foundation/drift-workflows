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
  manifest.json     # the service manifest: 5 named scripts + ONE shared deployment/routing registry
  deployment notes  # the `deployment` block inside manifest.json is the shared routing registry:
                    #   participants (payments / inventory / accounts), operation contracts, and
                    #   compensation bindings. (db + endpoints are placeholders to fill in.)
  workflows/
    payment_authorize_capture.mf       # authorize → capture → record_ledger  (2 compensations)
    payment_refund.mf                  # one idempotent corrective op
    inventory_reserve_release.mf       # reserve_inventory → commit_shipment  (1 compensation)
    account_adjustment_with_rollback.mf# adjust_balance → post_journal        (auto-rollback)
    checkout_branch_merge.mf           # BRANCH + MERGE + COMPENSATION in one flow
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
| account_adjustment_with_rollback | later-step failure → **automatic compensation** (workflow `reversed`) |
| payment_refund | idempotent single-op corrective flow |
| inventory_reserve_release | participant **pending → resume** completes |
| checkout_branch_merge | branch + merge + compensation runs end to end |
| (any) | manifest **reload** doesn't break a pinned older workflow; **terminal replay** with the participant down |

Toolchain: driftc 0.33.53 / abi 18. As-built design: `microflows/doc/microflows_design.md` §15 (service)
and §12 (the language/IR).
