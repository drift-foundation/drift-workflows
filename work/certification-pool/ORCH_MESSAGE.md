# Request: add `drift-workflows` to the certification pool

**From:** drift-workflows (Singular + Microflows)
**Ask:** wire one new `package_repo` entry and certify **both** artifacts together — **`microflows 0.1.0`**
and **`singular 0.5.0`**. The Bookkeeper-consumable Singular API reshape (the `0.5.0` work — caller-supplied
`std.time.UtcTimestamp` times, `WorkLease` threading, thrown `EventTimeConflict` / `InvalidLeaseExpiry`) is
**now implemented and green** (see §5); both packages are ready, and certifying them unblocks Bookkeeper.

We did NOT touch build-orchestrator. This is a request + the repo-side alignment that backs it. Everything
below is implemented and green locally on `0.33.53 / abi18`.

---

## 1. The entry (single `package_repo`, two artifacts)

```jsonc
{
  "path": "../drift-workflows",
  "kind": "package_repo",
  "depends_on": ["drift-lang", "drift-mariadb-client", "drift-net-tls", "drift-web"],
  "affects":    ["bookkeeper"],
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
- `depends_on`: the libraries depend on `mariadb-rpc`; the integration apps (runner/service/stub) also
  link `web` + `net-tls`. Edges set so a bump in any of those re-tests us.
- `affects`: Bookkeeper consumes both packages.

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

- **DB-serialized.** Every DB-backed job runs under flocker key `mariadb-mdb114-a` (your shared serial
  group) — the same discipline as drift-mariadb-client. Destructive schema resets never overlap another
  gate's DB use.
- **perf baselines are committed**, machine-keyed (`perf/baselines/<machine-id>.json`); `perf/results/`
  is gitignored. **A missing baseline HARD-FAILS** the gate (never auto-minted in a gate run; only an
  explicit `--update-baseline` records one, which is then committed). So on a fresh cert host, perf will
  fail until a baseline is recorded **on that host** and committed — by design. **Please tell us the cert
  host('s machine-id) so we commit its baseline** (or run `just <component> perf --update-baseline` once
  there and commit the result). Perf is timing/throughput at the library/API level with the real DB round
  trips in the workload; per-SP-call / wire-byte accounting stays in drift-mariadb-client.

## 4. Environment contract (what the gate host must provide)

- **MariaDB** reachable at `127.0.0.1:34114`; root password in `MDB_ROOT_PWD`. (Same instance/contract as
  drift-mariadb-client.)
- **flocker** + **`DRIFT_TOOLCHAIN_ROOT`** (the staged toolchain ≥ 0.33.17 provides both the executor and
  flocker).
- **Mariachi ≥ 1.0.0** — the integration `test`/`stress`/`perf` gates reset + seed the singular and
  microflows schemas via Mariachi. Today the recipes resolve it at `../../mariachi` (env override
  `MARIACHI_BIN`). **This is the one external tool we need you to provision** (or tell us the canonical
  path and we'll point at it). Everything else is in-repo or ships with the toolchain.

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

Repo-side alignment is complete and green locally: the four gates pass, and a root deploy stages both
packages under the Foundation key. **Both `microflows 0.1.0` and `singular 0.5.0` are ready to wire +
certify now** — the 0.5.0 reshape is implemented and verified (§5). The only settle-before-wiring items
are the env contract: **Mariachi** provisioning and the **perf-baseline cert host** (§3, §4).
