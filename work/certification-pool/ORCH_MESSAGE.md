# Request: add `drift-workflows` to the certification pool

**From:** drift-workflows (Singular + Microflows)
**Ask:** wire one new `package_repo` entry and certify **both** artifacts together — **`microflows 0.1.0`**
and **`singular 0.5.0`**. The Bookkeeper-consumable Singular API reshape (the `0.5.0` work — caller-supplied
`std.time.UtcTimestamp` times, `WorkLease` threading, thrown `EventTimeConflict` / `InvalidLeaseExpiry`) is
**now implemented and green** (see §5); both packages are ready, and certifying them unblocks Bookkeeper.

We did NOT touch build-orchestrator. This is a request + the repo-side alignment that backs it. Everything
below is implemented and green locally on **driftc 0.33.54 / abi 18** (see the Toolchain note next).

## 0. Toolchain — your choice (0.33.53 vs 0.33.54)

All gates (`just test` incl. integration 165/165, `just stress`, `just perf`) are **green on driftc
0.33.54 / abi 18** (staged). On **0.33.53** (the current certified default) note: the microflows parser
unit test used to be a single ~520-line function that inlined ~60 `.mf` scenarios into ONE driftc
translation unit — compiling it cost ~5 GB and **tripped a driftc codegen OOM on 0.33.53** (fixed in
0.33.54). We have since **reworked that test to be data-driven**: the built `microflows-runner` binary
reads `.mf` fixtures at runtime (`--parse-check`), so the heavy compile is **no longer on the gate path**
(it survives only as the off-path `just test-compiler-stress` canary). The cert gate therefore no longer
contains the OOM-triggering compile.

- **0.33.54 is the safe choice** (carries the codegen fix); we verified all gates on it.
- **0.33.53 likely works now** that the heavy compile is off-path, but we did **not** re-verify the full
  gate on it — your call.
- **abi 18 is unchanged either way**, so `drift/lock.json` + author-claim SCIs are identical regardless.

---

## 1. The entry (single `package_repo`, two artifacts)

```jsonc
{
  "path": "../drift-workflows",
  "kind": "package_repo",
  "depends_on": ["drift-lang", "drift-mariadb-client", "drift-web"],
  "requires":   ["tool:mariachi", "tool:docker"],
  "commands": {
    "test":           ["just", "test"],
    "stress":         ["just", "stress"],
    "perf":           ["just", "perf"],
    "stage_packages": ["{staged_drift}", "deploy", "--dest", "{libs_root}"]
  }
}
```

**Why ONE entry, not two.** The orchestrator clones `repo.path` as a Git root
(`git clone --no-checkout` → `git checkout <sha>`) and expects `drift/manifest.json` at the checkout root.
`singular/drift` and `microflows` are **subdirectories of the one `drift-workflows` Git repo**, not
independent Git roots — so per-subdir entries can't materialize as fresh checkouts. We added a **top-level
`drift/manifest.json`** declaring both artifacts (the drift-web / drift-mariadb-client convention:
multiple artifacts, one manifest, one deploy). They remain two individually-versioned artifacts with
separate author-claims.

- `stage_packages` is the **bare** deploy you specified — no cert-suite flags in the recipe (cert-suite
  policy is yours). The deploy emits cert claims, so it requires the **cert-suite evidence digest you
  supply** (via your `--cert-suite-*` / `DRIFT_DEPLOY_CERT_SUITE_*` mechanism); with that supplied, one
  `drift deploy --dest <libs_root>` from the checkout root stages **both** `singular/<v>` and
  `microflows/<v>` (verified locally with the empty-evidence sentinel). A deploy with NO evidence at all
  correctly refuses — as expected; we do not synthesize one.
- `depends_on`: our **direct** runtime foundations — the libraries depend on `mariadb-rpc`; the
  integration apps (runner/service/stub) link `web` (`web.rest`/`web.client`). We do **not** import
  `net-tls` directly (verified: no `net_tls` imports) — TLS sits *below* drift-web, so it's transitive via
  web, not a dependency we declare. **Upstream retest needs nothing extra from you:** the orchestrator
  derives downstream invalidation by **reversing `depends_on`**, so a bump in `drift-mariadb-client` or
  `drift-web` auto-retests drift-workflows (and `net-tls → web → workflows` flows transitively). No
  `affects` entries anywhere, and no upstream config edits.
- **No `affects` key.** The entry is exactly `path`, `kind`, `depends_on`, `requires`, `commands` — nothing
  else (a lingering `affects` is now a hard load-time error). Bookkeeper is a PushCoin consumer *outside*
  this pool — it consumes the certified snapshot and isn't validated by this orchestrator — so it's out of
  scope here regardless.

**Alternatives we did NOT take (your call if you prefer them):** (a) teach the orchestrator monorepo
/ sub-path package roots; (b) split into two Git repos. Both touch your config or our repo structure, so
we defaulted to the single-entry top-level-manifest model, which needs no orchestrator change.

## 2. Signing identity

Both artifacts are signed with the **Foundation key** (`ed25519:6DSIXZVQ…`, author + certifier) — same
identity as drift-web / drift-mariadb-client. `pushcoin.seed` (`ed25519:YvjbJdKV…`) is reserved for
`pushcoin/*` business apps and is NOT used here. The top-level `drift/trust.json` is unified to the one
Foundation key across all namespaces (`singular.*`, `microflows.*`, `mariadb.*`). No per-recipe sign
flags — the deploy resolves the configured Foundation key, exactly as your other Foundation repos do.

## 3. Gates (all bounded-but-REAL; a pass means "OK for real use")

| gate | what runs | meaning of green |
|------|-----------|------------------|
| `test`   | singular DB tests + microflows runner units + the **full coordinator↔singular e2e** (165 checks) | functional correctness end-to-end |
| `stress` | singular lease-contention (25×16 racers, exactly-one-winner) + coordinator concurrent-submit recovery race (20×8 racers, exactly-once dispatch) | concurrency/fencing correct under contention |
| `perf`   | singular acquire→settle→inspect throughput + coordinator service drive throughput, each gated vs a **committed baseline** | throughput has not regressed |

- **DB-serialized (our private instance).** Every DB-backed job runs under flocker key
  **`drift-workflows-mdb`** — serializing our own concurrent gate runs against our private MariaDB
  container so destructive schema resets never overlap. This is the lock the code resolves
  (`tools/cert-env.sh`, see §4); the orchestrator carries no lock key (it's ours to own).
- **perf baselines are committed**, machine-keyed (`perf/baselines/<machine-id>.json`); `perf/results/`
  is gitignored. **A missing baseline HARD-FAILS** the gate (never auto-minted in a gate run; only an
  explicit `--update-baseline` records one, which is then committed). So on a fresh cert host, perf will
  fail until a baseline is recorded **on that host** and committed — by design. **Please tell us the cert
  host('s machine-id) so we commit its baseline** (or run `just <component> perf --update-baseline` once
  there and commit the result). Perf is timing/throughput at the library/API level with the real DB round
  trips in the workload; per-SP-call / wire-byte accounting stays in drift-mariadb-client.

## 4. Environment contract — the `DRIFT_CERT_CAPABILITIES` capability model (ADOPTED)

We declare exactly TWO external **tool** capabilities — **`tool:mariachi`** (the schema tool) and
**`tool:docker`** (the container runtime). **MariaDB is NOT a platform service**: it's a **repo-PRIVATE
Docker fixture** (`tools/db_instance.sh`: a `mariadb:11.4` container `drift-workflows-mdb` on port
**34214**, repo-owned root password, **image pinned by digest** `sha256:2f45480c…`) that the **gate brings
up — and tears down — itself**, the same posture as drift-mariadb-client's own DB. So there is **NO
`service:mariadb`, NO injected DB endpoint, and NO injected secret**.

- **One root shim — `tools/cert-env.sh`** — sourced at the top of every DB-backed recipe, two-mode:
  - **cert mode** (`DRIFT_CERT_CAPABILITIES` set): authoritative for the two tools — read
    `tool:mariachi.bin` and `tool:docker.bin` (a missing capability/bin → the gate **fails early**, no
    silent fallback; **python3**, no `jq`). DB host/port/user/password are our own constants.
  - **local mode** (unset): `MARIACHI_BIN` defaults to `../mariachi/.venv/bin/mariachi`, `DOCKER_BIN` to
    `docker` on PATH (overrides honored). **No `MDB_ROOT_PWD` to supply** — `just test|stress|perf` need
    only `DRIFT_TOOLCHAIN_ROOT` + Docker.
- **The gate provisions AND restores entry state.** Each root gate (`test`/`stress`/`perf`) brings the
  private container up via `tool:docker` before any DB work, and — **if it started the container** — tears
  it **down on exit (success or failure, via a trap)**; if the container was already running (dev box), it
  is left as-is. No leftover container/port survives a gate. Inner schema-setup steps `up` idempotently
  (~8ms if running). The preflight verifies the docker *client*; daemon liveness is our `up` check.
- **ALL DB population goes through Mariachi — no `mariadb` client, no raw loader.** Product schemas
  (`singular`, `microflows`) AND test fixtures load via `mariachi apply`. The two formerly-raw fixtures are
  now their own **separate Mariachi-managed test schemas**: `singular_malformed` and `microflows_test` (the
  reversal seed proc, which writes into `microflows.*` with qualified names) — not in the product schemas
  (grants-ready). So the **external capability surface is exactly `tool:mariachi` + `tool:docker`** —
  nothing else beyond the staged toolchain/package-root + **flocker** (toolchain-provided).
- **DB serialization is ours** — flock key `drift-workflows-mdb`, serializing our own concurrent gate runs
  against the private instance.
- **Your side (orchestrator):** you've already wired it — `orchestration.json` declares `tool:docker` and
  drops `service:mariadb`, and `cert-env.example.json` resolves `tool:docker → /usr/bin/docker`. Just
  ensure the cert host's `cert-env.json` provides the docker path; we pin the `mariadb:11.4` image by
  digest, so no run-time pull — pre-provision that image on the host if you prefer (tell us and we'll
  coordinate).
- **Verified both modes** locally with a hand-written capabilities.json (onboarding §4 recipe): cert mode
  sources Mariachi + DB coords from the document; local mode runs off defaults — both green (§6).

## 5. Versions

Both ready; the final submission stages **`microflows 0.1.0` + `singular 0.5.0`** together from the
top-level manifest.

- **microflows `0.1.0`** — independent of singular in the dependency graph (the coordinator talks to
  MariaDB directly; only the test stub links singular, from source), so it can also land on its own.
- **singular `0.5.0`** — the Bookkeeper-consumable API reshape is **implemented and green**, NOT pending:
  - every durable lifecycle transition is **caller-clocked** — `start` / `complete` / `fail` /
    `extend_lease` take a caller `event_time: std.time.UtcTimestamp`; `start` / `extend_lease` also take
    an absolute `lease_expires_at: UtcTimestamp` (the public `lease_timeout_seconds` knob is gone). The
    gateway and the stored procedures never substitute DB time for a caller event time.
  - **strict monotonicity**: `event_time` must be strictly after the item's last recorded event, else a
    thrown **`EventTimeConflict`**; `lease_expires_at` must be strictly after `event_time`, else a thrown
    **`InvalidLeaseExpiry`** (enforced both client-side and in the SPs).
  - **`WorkLease` threading**: `WorkLease.lease_expires_at` is now a typed `UtcTimestamp`; `start` and
    `extend_lease` return the threaded `WorkLease` carrying the caller-approved deadline.
  - **behavior change to note**: extend is now caller-authoritative — it applies the proposed absolute
    deadline verbatim (validated `> event_time`); the pre-0.5 "never shorten the lease" flooring is gone
    (it would silently override the caller's absolute time). Max-horizon / clock-skew limits are
    deliberately deferred to a future caller-side/deployment policy.
  - verified: `singular` 16/16 (incl. the e2e lifecycle + raw-SQL SP-invariants for both throws) +
    lease-contention stress + drive-throughput perf; **integration 165/165** (the stub drives the real
    0.5 API). The top-level manifest stages `singular 0.5.0`, signed with the Foundation key.

## 6. Status

Repo-side alignment is complete and green, and the **`DRIFT_CERT_CAPABILITIES` capability model is
adopted** (§4). We verified **both modes** with a hand-written `capabilities.json` (onboarding §4):
- **cert mode** (`DRIFT_CERT_CAPABILITIES` set, Mariachi + DB sourced from the document, nothing assumed
  from ambient env): `just test` → integration **165/165**; `just stress` → exactly-once dispatch held;
  `just perf` → both packages within baseline. All exit 0.
- **local mode** (unset): same gates green off repo defaults.

A root deploy stages both packages under the Foundation key, and `drift trust check` passes
(✓ singular ✓ microflows). **Both `microflows 0.1.0` and `singular 0.5.0` are ready to wire + certify.**

Your side (no config edges needed; reversed-`depends_on` already covers upstream retest):
1. **Capabilities — already wired by you.** `orchestration.json` declares `tool:docker` and drops
   `service:mariadb`; `cert-env.example.json` resolves `tool:docker → /usr/bin/docker`. Our entry now
   reads `requires: ["tool:mariachi", "tool:docker"]`. Just have the cert host's `cert-env.json` provide
   both tool paths.
2. **Image:** we pin `mariadb:11.4` by digest (`sha256:2f45480c…`) so there's no run-time network pull;
   if you'd rather pre-provision that image on the cert host, say so and we'll coordinate.
3. The committed **perf baseline for the cert host's machine-id** (§3).

Re-run cert whenever ready.
