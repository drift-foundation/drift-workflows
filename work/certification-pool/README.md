# Certification-Pool Readiness — drift-workflows

## Objective

Get `drift-workflows` into the build-orchestrator cert pool so **`singular`** and **`microflows`** are
published as **certified client-packages** (built + certified against the current certified toolchain,
driftc 0.33.53 / abi 18), consumed exactly like `mariadb-rpc` / `web-rest`. This unblocks **Bookkeeper**,
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
- **DB-backed gates are supported**: the certified `drift-mariadb-client` runs its test/stress/perf
  against MariaDB at `127.0.0.1:34114`, serialized on a shared flocker DB-group key
  (`mariadb-mdb114-a`), password via `MDB_ROOT_PWD`. We use the identical contract.

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
  "depends_on": ["drift-lang","drift-mariadb-client","drift-net-tls","drift-web"],
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
  external deps are just drift-mariadb-client (mariadb-rpc, used by both libs + singular-from-source) +
  drift-net-tls + drift-web (web-rest/web-client, for the runner/service/stub HTTP apps in the gate).

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

### Sequencing vs the singular 0.5.0 reshape

The single entry certifies whatever the top-level manifest declares. To avoid certifying singular 0.4.x
as a Bookkeeper target, the recommended sequencing: get the repo **cert-ready now** (gates green,
top-level manifest, deploy works) but have the cert team **add drift-workflows to `run-all-latest` once
singular's 0.5.0 reshape lands**, so the certified `singular` is the reshaped surface. microflows is ready
in parallel and ships in the same first cert. (Open: whether to land microflows + singular-0.4.x first
and bump singular to 0.5.0 in a follow-on cert — faster microflows unblock, but stages a non-target
singular. Flagged for the team.)

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
5. **Make the DB/environment contract explicit + deterministic** (MariaDB `127.0.0.1:34114`,
   `MDB_ROOT_PWD`, flocker `mariadb-mdb114-a`; microflows needs BOTH the microflows control schema (via
   **Mariachi** — resolve its availability) AND the singular schema for the stub), matching
   drift-mariadb-client. Document for the orchestrator.
6. Per-component lockfiles already **regenerated via `drift prepare`** (this session); author-claims
   committed; deploy works from committed source. Keep it that way; add the top-level lock the same way.

## What `stress` / `perf` mean here (bounded but REAL — a pass must mean "OK for real use")

Bounded/smoke in SIZE, not in meaning. A green **stress** run must mean *concurrency is correct under
contention*; a green **perf** run must mean *throughput has not regressed* — caught against a
**persisted, committed baseline** so a cascading slowdown is a HARD FAILURE (the drift-mariadb-client
convention: a `perf_baseline.py`-style harness + committed `perf/results/`, gated on **op / SP-call /
round-trip / byte counts**, not wall-clock, so a busy host doesn't false-fail).

- **singular.stress** — N concurrent workers contend on the SAME lease key + replay the SAME idempotent
  operation; assert effectively-once + no lost/duplicated settle under contention. Serialized on the DB
  group, bounded iterations.
- **singular.perf** — fixed acquire→settle→inspect cycle; **pin** protocol round-trips / SP calls / bytes
  as a committed baseline; gate every future run against it.
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

- **MariaDB** at `127.0.0.1:34114`, root password via `MDB_ROOT_PWD`, schema applied by the gate (singular
  via its `db-load-schema`; microflows via Mariachi). Identical environment to the certified
  drift-mariadb-client gate → already orchestrator-supported.
- **flocker** DB-group serialization on `mariadb-mdb114-a` so concurrent cert lanes never collide on the
  one DB (the established convention).
- **No external services**: microflows's integration/stress boot the participant-stub + microflows-service
  themselves on ephemeral ports; nothing outside the repo is required.

## Version / package names Bookkeeper pins

- `microflows` → **0.1.0**.
- `singular` → **the reshaped API as a NEW version (≥ 0.5.0)** — **NOT 0.4.1**.

**DECISION (made):** do **not** certify 0.4.1 as the Bookkeeper target. The parked determinism reshape
changes singular's public caller contract enough that 0.4.1 would be a churn release; the app team wants
to adapt §5 **once** against the stable surface. So the **official, Bookkeeper-consumable `singular`
package is the reshaped surface**, published as a new version (a material API change → bump the version,
not a re-tag of 0.4.1). The reshape is the **next singular-specific sub-step**, BLOCKING official
cert-pool submission of singular. See "Singular reshape sub-step" below.

> Sequencing: the repo-side, **API-agnostic** cert alignment (this lane's bulk — command contract,
> deploy/stage_packages, real stress/perf, DB env, layout/claims/locks) proceeds NOW and is valid
> regardless of the API. `microflows` can be submitted to the pool once aligned. `singular` aligns now
> but its official submission waits on the reshape version.

## Singular reshape sub-step (blocks official singular submission)

A separate singular-specific change, scoped here but implemented as its own step (needs the design/spec
from the singular owners — this lane does not guess the API):

- caller-supplied `std.time.UtcTimestamp` (deterministic time in, no hidden clock reads);
- `WorkLease` threading through the gateway API;
- thrown `EventTimeConflict`;
- thrown `InvalidLeaseExpiry`.

Deliverable of that sub-step: the reshaped singular API at a new version, its tests/stress/perf updated,
re-signed + lock-regenerated, then submitted as the Bookkeeper-pinned package. Microflows consumes
singular only in its test apps (stub), so it tracks whatever singular version is current; the published
microflows **library** is unaffected by the singular API (no singular dep).

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
