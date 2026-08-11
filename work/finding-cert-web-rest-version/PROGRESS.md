# Progress — cert web-rest 0.7 adoption

Single writer: `workflows.implementer`.

## Status

**SIGNED OFF for certification rerun** by review `2026-08-11T21-15-51Z` — no
remaining code-review findings. Steps 1-9 done and green; the certification rerun
is the only remaining work and it is Slawomir's gate.

This is certification readiness, **not** terminal closure. Acceptance criterion 2
stays open until the rerun's test/stress/perf gates pass in normal and debug. If
the rerun fails, the new run summary and failing log come back to this finding;
if it passes, close the folder after merge per the ephemeral-finding policy.

| Step | State |
| --- | --- |
| 1. Independent inspection | done |
| 2. Sequencing ruling | resolved — adopt 0.7 now |
| 3. Bump constraint (3 files, 4 declarations) | **done** |
| 4. Release integrity (0.8.1, RUNNER_VERSION, changelog, author-claim) | **done** |
| 5. Relock (4 pins, 3 files) | **done** — only web-rest moved |
| 6. Compile | **done** — all three artifacts green |
| 7. Service integration on 0.7.0 | **done** — 231/231 passed |
| 8. Trust / reseal checks | **done** — green |
| 9. Correct stale justfile staging comment | **done** |
| 10. Cert gates | not run — Slawomir's gate (only open item) |

## Blocker cleared by reviewer correction `4aaf0177...`

My "0.7.0 is published nowhere" claim was **wrong**, and the reviewer disproved
it. I had scoped the search to `/home/sl/opt/drift`; the run package root lives
under `build-orchestrator/build/runs/`. Verified directly:

    /home/sl/src/build-orchestrator/build/runs/20260811-193433-drift-lang-b60b86d/pkgs

holds signed `web-rest/0.7.0` with SCI
`f59004d190744de7eda4151fb626d27fb06ab4c2835394dc8edfdc19af964071` in both its
cert-claim and author-claim — and every other dep we need resolves there too
(`mariadb-rpc 0.8.2`, `mariadb-wire-proto 0.6.2`, `microflows 0.9.0`,
`net-tls 0.6.4`, `singular 0.10.0`, `web-client 0.5.5`, `web-jwt 0.5.4`). No
producer prerequisite existed. Everything below was done with `DRIFT_PKG_ROOT`
set to that root.

## What landed

**Step 3 — four declarations, three files** (driven off the declaration count, so
the runner manifest's second entry was not missed):

    drift/manifest.json                              1  "0.6" -> "0.7"
    microflows/runner/drift/manifest.json            2  "0.6" -> "0.7"
    microflows/participant-stub/drift/manifest.json  1  "0.6" -> "0.7"

**Step 4 — release integrity:**

- `drift/manifest.json`: `uflowsd` `0.8.0` -> `0.8.1`
- `microflows/runner/src/runner.drift:54`: `RUNNER_VERSION` -> `"0.8.1"`
- `microflows/CHANGELOG.md`: new self-contained `uflowsd 0.8.1` entry
- `drift/uflowsd.author-claim` re-minted: version `0.8.1`, new
  `source_content_id sha256:9cac0f22...`, `release_utc 2026-08-11T20:43:29Z`

**Step 9:** `justfile` staging comment corrected (comment text only).

## Verification performed

**Relock (step 5) — the stop condition held.** Exactly the predicted diff: four
`web-rest` pins across three lock files moved `0.6.5` -> `0.7.0`
(`7fbf466b95f1` -> `1d34159f0ede`), and **no other pin moved in any file**
(root 1/11 pins changed, runner 2/12, participant-stub 1/4).

**Compile (step 6) — zero migration source edits.** All three artifacts built
against `web-rest@0.7.0`:

| Artifact | Result | Time |
| --- | --- | --- |
| `microflows-runner` (`mfrunner`) | exit 0 | 1m34s |
| `uflowsd` (entry `microflows.runner::service_main`) | exit 0 | 1m34s |
| `microflows-participant-stub` | exit 0 | 36s |

The only source edit in the whole migration is `RUNNER_VERSION`, exactly as the
`FINDING.md` inference predicted. Version stamps confirm on the real binaries:
`mfrunner 0.8.1` and `uflowsd 0.8.1`.

**Trust / reseal (step 8):** `just reseal` exit 0 — author-claims minted,
lock re-resolved, `OK: drift/manifest.json is trust-v1 ready` with all three
artifacts checked.

**Service integration (step 7): `231/231 passed`, zero failures.**
`just test-integration` (coordinator-singular) against the MariaDB fixture, built
from source on 0.7.0. The assertions the review named are all present and green:

    PASS  admission_draining_refuses_fresh_submission
    PASS  admission_draining_resume_converges_pending_restart
    PASS  service_draining_refuses_503
    PASS  service_draining_resume_pending_restart_503

The rest of the HTTP service lane is green too — `service_health_ready`,
`service_submit_completes`, `service_submit_pending_202`,
`service_unknown_script_400`, `service_malformed_body_400_no_row`,
`service_resume_replays_terminal`, `service_sigusr1_reload_swaps_registry`.

## Standing caveat

0.7.0 changed response framing and connection ownership beneath an unchanged call
surface, and `pool_framing_test` had previously passed on client leniency while
the wire was malformed. A green integration run is necessary but not sufficient
evidence about wire behavior; the cert lane's own gates remain the authority.

## Not run here

Repository `stress` and `perf` gates, and the certification rerun. The cert rerun
is Slawomir's gate by policy and was not invoked. `just test-integration` was the
focused gate the review asked for.

Review `2026-08-11T20-58-13Z` ruled the local stress/perf sweep **not required**
before re-review: the certification rerun is the authority, and machine-capacity
guidance asked teams to avoid redundant heavy gates. Recorded so a later reader
does not mistake the gap for an oversight.

## Toolchain correction — all evidence re-derived on the run toolchain

The first verification pass used the **certified** toolchain (driftc 0.35.0). The
cert lane uses the run's sibling toolchain, **driftc 0.36.0** (`b60b86dd`), so the
first pass did not match the lane it was evidence for. Everything was re-run with
`DRIFT_TOOLCHAIN_ROOT` set to
`build-orchestrator/build/runs/20260811-193433-drift-lang-b60b86d/toolchain`.

Result: identical, all green.

| Artifact | 0.35.0 (first pass) | 0.36.0 (run toolchain) |
| --- | --- | --- |
| `microflows-runner` | exit 0, 1m34s | exit 0, 1m32s |
| `uflowsd` | exit 0, 1m34s | exit 0, 1m31s |
| `microflows-participant-stub` | exit 0, 36s | exit 0, 35s |
| integration suite | 231/231 | 231/231 |

`cert_deps.py --check-floor` passes on 0.36.0 (repo floor is 0.35.0). The
definitive artifact evidence is uflowsd's own build-info, read through the
supported interface:

    driftc 0.36.0, git b60b86dd, abi 22
    web-rest 0.7.0

That single document ties the artifact, the toolchain and the dependency together
— it is stronger evidence than the build logs, and it is what I should have
produced in the first pass.

## Review round 2026-08-11T20-58-13Z — both items applied

**P1 — author claims restored.** `drift/microflows.author-claim` and
`drift/singular.author-claim` are back to their pre-work bytes, read out of
history with `git show HEAD:<path>` and written directly — no mutating git
operation, per the review's instruction and the repository's git rules. Neither
artifact changed version, dependencies, or source content identity, so the
re-dating was unrelated release churn. `drift/uflowsd.author-claim` stays
re-minted (version `0.8.1`, new SCI `sha256:9cac0f22...`, `web-rest 0.7`
dependency) because that artifact did change.

Re-verified after the restore: `just trust-check` green on all three artifacts,
`git diff --check` clean. `drift/uflowsd.author-claim` is now the only claim file
in the diff.

**P2 — durable record corrected.** `FINDING.md` no longer carries the disproved
"0.7.0 is published nowhere" claim under a `Confirmed` heading; it is struck
through at the point of use and moved to an explicit **Disproved hypothesis**
section naming the search-scope error and the actual run package root. The
zero-source-change section is relabelled from Inferred to **Confirmed by build**.
Acceptance criteria now carry per-item status, including the reviewer's
independent four-artifact `drift lock emit --source-rebuild` result. `PLAN.md`
drops the bogus 2b prerequisite, marks steps 3-9 DONE, and reduces step 10 to the
certification rerun.

## Uncommitted worktree

    drift/manifest.json                              uflowsd 0.8.1 + web-rest 0.7
    drift/lock.json                                  relocked
    drift/uflowsd.author-claim                       re-minted (0.8.1)
    microflows/CHANGELOG.md                          new 0.8.1 entry
    microflows/runner/src/runner.drift               RUNNER_VERSION 0.8.1
    microflows/runner/drift/manifest.json            2 declarations -> 0.7
    microflows/runner/drift/lock.json                relocked
    microflows/participant-stub/drift/manifest.json  1 declaration -> 0.7
    microflows/participant-stub/drift/lock.json      relocked
    justfile                                         staging comment corrected

Ten files, plus the untracked finding folder. `drift/microflows.author-claim` and
`drift/singular.author-claim` are NO LONGER in the diff — restored per review P1.
The `AGENTS.md` / `AGENTS-MAILBOX-PROTO.md` changes in the worktree pre-date this
finding and are unrelated to it.

Nothing staged or committed; the git index remains Slawomir's.

## Literal next action

**Slawomir's full certification rerun.** Reviewer re-check is complete and signed
off; nothing else is pending on either role. Per the review, the local
repository stress/perf sweep is **not** required before re-review: the rerun is
the authority, and machine-capacity guidance asked teams to avoid redundant heavy
gates. Terminal closure of this finding requires that rerun to pass.
