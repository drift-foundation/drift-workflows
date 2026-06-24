# Certification-Pool Readiness — drift-workflows

## Objective

Get `drift-workflows` into the build-orchestrator cert pool so **`singular`** and **`microflows`** are
published as **certified client-packages** (built + certified against the certified toolchain — gates
green on driftc **0.33.54 / abi 18**; 0.33.53 has a codegen OOM the now-data-driven parser gate sidesteps,
cert-team's toolchain choice — see ORCH_MESSAGE §0), consumed exactly like `mariadb-rpc` / `web-rest`.
This unblocks **Bookkeeper**,
which is gated on a certified `singular` (its dist left the monorepo with the repo move and is not in
the pool; the only pool `singular` is a stale 0.3.1 pre-move snapshot).

**Hard constraint (from the directive):** we do **NOT** edit build-orchestrator. We align THIS repo to
the orchestrator contract, dry-run certification locally, then hand the orchestrator team a message +
the exact proposed entries for them to add. Locally-published `build/dist/lib` artifacts are dev tooling,
**never** certification output.

## The contract (read from build-orchestrator, do not edit)

`~/src/build-orchestrator/{orchestration.json, run-all-latest.json, docs/orchestrator-schema.md}`.

- A repo entry is `{ path, kind, depends_on[], affects[], commands{} }`. `kind ∈ {toolchain,
  package_repo, app_repo}`. Cert targets are `package_repo`s.
- A `package_repo` exposes four commands the orchestrator invokes:
  - `test`  — correctness gate (`just test`)
  - `stress`— stability/contention gate (`just stress`)
  - `perf`  — perf-regression gate (`just perf`)
  - `stage_packages` — **bare** `{staged_drift} deploy --dest {libs_root}` (publishes every artifact in
    the project's `drift/manifest.json`).
- **Cert-suite policy is orchestrator-owned.** Project recipes/commands **must NOT** pass
  `--cert-suite-id` / `--cert-suite-evidence-sha256` / `--cert-suite-no-evidence`; the orchestrator
  appends them (`stage` → `--cert-suite-no-evidence`; `release` → `--cert-suite-evidence-sha256`). Our
  local `just deploy` may inject a dev default *only when ARGS carries no `--cert-suite`* (the
  drift-mariadb-client / drift-web pattern) — already how `singular/drift/justfile` works.
- The orchestrator sets `DRIFT_TOOLCHAIN_ROOT` (staged toolchain) and `DRIFT_PKG_ROOT` (staged
  `libs_root`, populated by upstream `stage_packages`). Recipes must rely only on these, not on ambient
  local dist.
- **DB-backed gates are self-provisioned**: like `drift-mariadb-client`, MariaDB is a **repo-private**
  test fixture, not a platform service. Our gate brings up (and tears down) its own `mariadb:11.4`
  container (`tools/db_instance.sh`, `127.0.0.1:34214`) and serializes on its own flocker key
  (`drift-workflows-mdb`). The container runtime is the declared `tool:docker` capability, so the
  external surface is `requires: ["tool:mariachi", "tool:docker"]`.

## Proposed orchestrator entry — ONE `drift-workflows` `package_repo` (multi-artifact)

**Corrected (cert-team review):** the orchestrator treats `repo.path` as a **Git clone root**
(`git clone --no-checkout <path>` → `git checkout <sha>`) and looks for `drift/manifest.json` **at the
checkout root**. `singular/drift` and `microflows` are **subdirectories of one Git repo** (`git
rev-parse --show-toplevel` → `drift-workflows` for both), so per-subdir entries cannot materialize as
checkouts. So we use the convention every existing cert repo uses — **one entry per Git repo, multiple
artifacts staged from a single top-level manifest** (drift-web stages 4 packages, drift-mariadb-client 2,
from one `drift/manifest.json` + one `deploy`):

```jsonc
// (proposed — for the orch team to add; we do not edit their config)
"drift-workflows": {
  "path": "../drift-workflows",
  "kind": "package_repo",
  "depends_on": ["drift-lang","drift-mariadb-client","drift-web"],
  "commands": {
    "test":  ["just","test"], "stress": ["just","stress"], "perf": ["just","perf"],
    "stage_packages": ["{staged_drift}","deploy","--dest","{libs_root}"]
  }
}
```

This requires a **new top-level `drift/manifest.json` at the repo root** declaring BOTH library artifacts
(the drift-web pattern), each individually versioned + author-claimed:
- `singular` — the reshaped API at a **new version (0.5.0)**; depends only on `mariadb-rpc`.
- `microflows@0.1.0` — library + coordinator depend only on `mariadb-rpc`.

"Two distinct packages" is preserved: they are two **artifacts** (two versions, two author-claims,
individually pinnable by Bookkeeper), just staged from one repo entry — exactly like drift-web's four.

- Root `just test/stress/perf` already aggregate the per-component gates (singular → microflows →
  integration); they run from the checkout root, satisfying the contract.
- `drift deploy --dest {libs_root}` from the root reads the **top-level** manifest and stages both
  libraries. The per-component `drift/` projects stay for local dev; the cert entry point is the new
  top-level manifest.
- `bookkeeper` `depends_on` `["drift-workflows"]` is NOT right (it pins *packages*, not repos) — it pins
  the certified **`singular` 0.5.0** + **`microflows` 0.1.0** artifacts the drift-workflows entry stages.
- **microflows has no `singular` package edge:** the coordinator never calls singular; only the
  participant-stub *test double* uses it, compiled **from source** in the integration. So the repo's
  **direct** external deps are just drift-mariadb-client (mariadb-rpc, used by both libs + singular-from-
  source) + drift-web (web-rest/web-client, for the runner/service/stub HTTP apps in the gate). We do NOT
  consume `net-tls` directly (it sits below drift-web — transitive, not declared). Upstream retest needs
  the cert team to add `drift-workflows` to `drift-mariadb-client.affects` + `drift-web.affects` (the
  orchestrator computes downstream invalidation from `affects`, not `depends_on`); TLS flows via the
  existing `drift-net-tls → drift-web → drift-workflows` chain.

> The participant-stub / microflows-runner / microflows-service are **apps**, built+booted *inside* the
> test/stress gates (HTTP, ephemeral ports). They are NOT cert-pool packages and are NOT in the manifest.

### Alternatives flagged to the cert team (their call — we do not edit their config)

1. **Monorepo support** — if singular + microflows must be *independently addressed* entries from one Git
   repo, the orchestrator needs clone-root + package-subpath per command + per-artifact author-claim
   preflight + stage. That **requires editing build-orchestrator** → contradicts our constraint → ask the
   cert team before assuming.
2. **Split into two Git repos** — also satisfies the current model, but a much larger repo-structure
   decision; not assumed here.

We proceed with the single-entry shape (option 1 above) under the current constraint.

### Singular 0.5.0 reshape — DONE; certify 0.5.0 only

The reshape is **implemented and green** (caller-supplied `UtcTimestamp` event times on all transitions;
thrown `EventTimeConflict` / `InvalidLeaseExpiry`; `WorkLease` threading). The top-level manifest stages
**`singular 0.5.0`** (NOT 0.4.x — that version is never submitted). Both artifacts certify together in the
first cert: `singular 0.5.0` + `microflows 0.1.0`. No sequencing question remains — there is no scenario
that stages a pre-reshape singular.

## Command surface this repo will expose (from the repo ROOT — the checkout root)

| Command (orchestrator) | What runs |
|---|---|
| `just test` | root justfile aggregates: singular test → microflows test (unit + **full DB-backed e2e** integration, real HTTP) |
| `just stress` | **NEW** singular lease-contention/idempotent-replay + **NEW** microflows concurrent-drive/effectively-once |
| `just perf` | **NEW** singular acquire→settle counts + **NEW** microflows workflow-drive counts, each **vs a committed baseline** |
| `stage_packages` | bare `{staged_drift} deploy --dest {libs_root}` reads the **top-level `drift/manifest.json`**, stages `singular` + `microflows` |

## Gaps to close (repo-side, this lane)

1. **NEW top-level `drift/manifest.json` (+ `drift/{singular,microflows}.author-claim`, `lock.json`,
   `trust.json`) at the repo root** declaring both libraries, so the orchestrator's root-level `drift
   deploy --dest` stages both (the drift-web pattern). Module paths reach into `singular/drift/packages/`
   and `microflows/packages/`. Author-claims re-minted over the top-level source (6DSIXZVQ identity).
2. **`singular/drift` has NO `stress`/`perf` recipes** (only `test`/`deploy`). Add them; the root justfile
   already calls them.
3. **`microflows` `stress`/`perf` are echo-only stubs.** Replace with real bounded gates + committed
   baselines (not placeholders, per directive).
4. **Verify the bare `drift deploy --dest <tmp>` contract** from the repo root (top-level manifest +
   author-claims resolve; no `--cert-suite` hardcoded; deps from `{libs_root}` + toolchain).
5. **Make the DB/environment contract explicit + deterministic**: MariaDB is a **repo-private** Docker
   fixture the gate provisions itself (`tools/db_instance.sh`, `127.0.0.1:34214`, flocker
   `drift-workflows-mdb`); schema population is via **Mariachi** (microflows needs BOTH the microflows
   control schema AND the singular schema for the stub). External surface is
   `requires: ["tool:mariachi", "tool:docker"]` — the schema tool + the container runtime; MariaDB
   itself is the repo-private fixture, not `service:mariadb`. Document for the orchestrator.
6. Per-component lockfiles already **regenerated via `drift prepare`** (this session); author-claims
   committed; deploy works from committed source. Keep it that way; add the top-level lock the same way.

## What `stress` / `perf` mean here (bounded but REAL — a pass must mean "OK for real use")

Bounded/smoke in SIZE, not in meaning. A green **stress** run must mean *concurrency is correct under
contention*; a green **perf** run must mean *throughput has not regressed* — caught against a
**persisted, committed baseline** so a slowdown is a HARD FAILURE. Our packages measure at the
**library/API level** with the **real DB round trips in the workload** (production behavior), and gate
**elapsed time / throughput** vs a committed, machine-keyed baseline — with a **missing baseline as a
hard failure** (never auto-minted in a gate; only `--update-baseline` records, then it's committed).
Per-SP-call timing / wire-byte accounting is **drift-mariadb-client**'s domain, not ours. (A tolerance
absorbs host variance while catching cascading slowdowns; logical counts like exactly-once dispatch are
asserted as **correctness** in stress/test, not as the perf metric.)

- **singular.stress** — N concurrent workers contend on the SAME lease key + replay the SAME idempotent
  operation; assert effectively-once + no lost/duplicated settle under contention. Serialized on the DB
  group, bounded iterations.
- **singular.perf** — fixed acquire→settle→inspect cycle measured at the **library/API level** (the real
  gateway → mariadb-rpc → SP → MariaDB round trips ARE in the workload — production behavior). Gate
  **elapsed time / throughput** (`per_cycle_us`) against a committed, machine-keyed baseline; a **missing
  baseline HARD-FAILS** (only `--update-baseline` records — never auto-minted during a gate). Singular
  does NOT measure per-SP-call timing or wire-byte accounting — that is **drift-mariadb-client**'s domain.
- **microflows.stress** — concurrent drives of one workflow id (recovery race) + respond-pending→resume
  under contention; assert exactly-once dispatch + clean terminal state.
- **microflows.perf** — drive a fixed workflow to completion; **pin** participant-dispatch count + event
  count (+ DB op count) as a committed baseline; gate against it.

All four: DB-backed, serialized on the shared flocker DB key, deterministic, with **committed baselines**
(checked in under each project's `perf/`) that make a regression fail the gate.

### Confirmed decisions (pinned)

1. Reshaped `singular` ships as **0.5.0** (new version, never a re-tag of 0.4.1).
2. microflows cert **`test` = the FULL e2e** coordinator↔singular integration (165 checks incl. the
   C21/C22 starter-kit examples) — a pass means "prod-usable for real app dev/use."
3. **Two distinct packages** (singular, microflows) — packaged individually now, possibly separate repos
   later; never bundled into one entry.
4. Perf carries **persisted, committed baselines + regression gating** (catch cascading slowness), not a
   bare smoke run. Bounded/real stress: a pass means concurrency + throughput are OK for real use.
5. The singular **reshape** (UtcTimestamp / WorkLease / EventTimeConflict / InvalidLeaseExpiry → 0.5.0) is
   a separate sub-step, picked up against its spec; API-agnostic alignment proceeds now.

## DB / service dependencies (explicit for the orchestrator)

- **MariaDB is repo-PRIVATE — NOT a platform `service:mariadb`.** The gate brings up (and tears down) its
  own `mariadb:11.4` container (`tools/db_instance.sh` → `drift-workflows-mdb` on `127.0.0.1:34214`, image
  pinned by digest, repo-owned root password) and populates it via Mariachi. Auto-provisioned (idempotent)
  at each schema-setup step and torn down on gate exit (restoring entry state); `just db-up`/`db-down`/
  `db-status` give explicit control. No injected DB endpoint, no injected secret. The container runtime is
  the **declared `tool:docker` capability** — so the external surface is
  `requires: ["tool:mariachi", "tool:docker"]`.
- **flocker** serialization on our own key `drift-workflows-mdb` so our concurrent gate runs never collide
  on the private instance.
- **No external services**: microflows's integration/stress boot the participant-stub + microflows-service
  themselves on ephemeral ports; nothing outside the repo is required.

## Version / package names Bookkeeper pins

- `microflows` → **0.1.0**.
- `singular` → **the reshaped API at 0.5.0** — **NOT 0.4.x** (never submitted).

**DECISION (done):** certify **`singular 0.5.0` only** as the Bookkeeper target. The determinism reshape
changed singular's public caller contract (a material API change → a new version, not a re-tag), so 0.4.x
is never submitted. The reshape is **implemented and green** (see below); both artifacts certify together.

## Singular 0.5.0 reshape — IMPLEMENTED + GREEN

The reshape landed (owner gave the spec + directed implementation). The 0.5.0 public surface:

- caller-supplied `std.time.UtcTimestamp` event time on **every** mutating transition
  (`start`/`complete`/`fail`/`extend_lease`) + absolute `lease_expires_at` on start/extend (the public
  `lease_timeout_seconds` knob is removed); the gateway + SPs never substitute DB time for a caller time;
- strict monotonicity → thrown **`EventTimeConflict`** (errno 30002); `lease_expires_at > event_time`
  → thrown **`InvalidLeaseExpiry`** (errno 30003); enforced client-side AND in the SPs;
- `WorkLease.lease_expires_at` typed as `UtcTimestamp`; `start`/`extend_lease` return the threaded lease;
- behavior change: extend is caller-authoritative (proposed absolute deadline applied verbatim,
  validated `> event_time`); the pre-0.5 "never shorten" flooring is gone.

Verified: singular 16/16 (e2e + raw-SQL SP-invariants for both throws + complete/fail/extend
monotonicity), stress, perf; integration 165/165 (stub on the 0.5 API); top-level deploy stages
`singular 0.5.0`, Foundation-signed. Microflows consumes singular only in its test stub (compiled from
source); the published microflows **library** has no singular dep, so it is unaffected by the singular API.

## Implementation order (repo-side first, then the message)

1. Add `singular/drift` `stress` + `perf` (bounded; emit_test_plan extensions + scenario sources +
   committed baseline). Root `just stress/perf` already call them.
2. Replace `microflows` `stress` + `perf` stubs with bounded gates + committed baselines.
3. **Create the top-level `drift/manifest.json` (+ author-claims, lock, trust) at the repo root**
   declaring `singular` + `microflows`; `drift prepare` the top-level lock; verify a bare
   `drift deploy --dest <tmp>` from the ROOT stages both (the orchestrator's stage_packages shape).
4. Make the DB env contract explicit in each gate; resolve the Mariachi availability question.
5. **Local dry-run** from a clean state with `DRIFT_TOOLCHAIN_ROOT`/`DRIFT_PKG_ROOT` set to staged roots:
   `just test`, `just stress`, `just perf` (from the root), and the bare root `drift deploy --dest
   <tmp-libs>` — all green, no ambient-dist dependence.
6. Craft the orchestrator-team message (the single `drift-workflows` entry + dep/affects edges + the
   monorepo-vs-split call-out + env/Mariachi contract + version pins + 0.5.0-sequencing). Hand it over;
   do not edit their config.

## Verification target

- Repo gates green locally (`test`/`stress`/`perf` per project).
- Bare `deploy --dest` stages singular + microflows cleanly from committed source.
- Evidence that drift-workflows can pass `test`, `stress`, `perf`, `stage_packages` under a staged
  toolchain + libs root (no ambient `build/dist/lib`).

## Constraints honored

No orchestrator edits · no hand-edited lock hashes/author-claims (regenerated via `drift prepare`) · local
`build/dist/lib` is not cert output · explicit/deterministic DB env · no empty stress/perf · starter-kit
examples stay (they run in microflows's gate when its DB/HTTP infra is satisfied).

## Status / next action

Status: **plan revised to the single-entry model (cert-team review folded in).** Next: implement the
stress/perf gates (1–2), the top-level cert manifest (3), DB/Mariachi contract (4), dry-run (5), message
(6). See `PROGRESS.md`.
