# script-registry — Progress / status

Design + plan live in [README.md](./README.md); this is the at-a-glance status.

## Status: **SCOPING** (charter agreed; implementation not started)

Approved: ScriptRegistry as the next milestone; the per-step `plan_hash` pin
evolves into the registry's `(revision, content_hash)` identity. IR stays
hand-built; parser deferred.

## Sub-step ledger
- [ ] **A — script revision identity + registry lookup.** `ScriptIR` +
  `ScriptRegistry` interface; in-process immutable registry keyed by
  `(name, revision)` with `content_hash`; runner resolves IR via the registry;
  fold `tb_mf_workflow_plan.plan_hash` → `content_hash`.
- [ ] **B — deployment/activation, no mutation of existing revisions.** Staged
  atomic build/activate; rollback = re-activate a prior revision.
- [ ] **C — runner loads the PINNED IR**, state-sensitive: durable `plan_get` FIRST →
  create only if absent (race-safe) → claim/inspect → resolve. An existing workflow
  never reads the active registry; absent/mismatched pinned revision → durable defer
  AFTER claim (holds a fence), never substitute; terminal replays from durable state
  (incl. `plan_length`) without the IR. `content_hash` = versioned SHA-256 (not MD5).
- [ ] **D — restart + rollback tests** across registry updates: rollback proves the
  active pointer moved (new pins N, existing stays N+1); never-substitute safety;
  terminal replay without IR.

## Next action
Begin sub-step A: define `ScriptIR` + `ScriptRegistry`, stand up the in-process
immutable registry, and switch the runner's `_build_plan` → `registry.resolve`
while folding the workflow pin from `plan_hash` to the revision `content_hash`.

## Verification
Full root `just test` green; the existing reversal/compensation suite (A–D)
unchanged and green throughout (machinery reused, not rewritten).
