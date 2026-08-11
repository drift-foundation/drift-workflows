# Finding: cert source-rebuild cannot satisfy `web-rest 0.6` after drift-web staged 0.7.0

Filed: 2026-08-11
Owner: `workflows.implementer`
Origin: Baton `review_request` `040e1d3d4d2372e620cf64892dc3af6f` from `workflows.reviewer`
Status: **implemented and verified — awaiting certification rerun.** The migration
is in the working tree, compiles, and passes the focused integration lane and the
four-artifact source-rebuild check. Terminal closure requires Slawomir's
certification rerun to pass.

## Observed

The certification gate fails for `uflowsd` in `--source-rebuild` mode: the
`web-rest 0.6` constraint is unsatisfiable.

## Confirmed (code-path facts)

Producer side — `web.source` @ `558b647`:

- `drift/manifest.json:34` declares `web-rest` **0.7.0** (drift-web keeps one
  repo-level manifest listing its packages, not a per-package one).
- The certified snapshot `20260806-114417-drift-lang-1c8cdd9` pinned drift-web at
  `81924e60` (the 0.35.0 adoption commit); HEAD is two commits ahead
  (`3619abe` cleanup, `558b647` the 0.7.0 bump).
- `certified/current/pkgs/web-rest/` contains **0.6.5 only**.
  `staged/libs/` contains no `web-rest` at all (only `or-throw-probe`).

Consumer side — `workflows.source` @ `af0ab2f`:

- Four manifest declarations request `{ "name": "web-rest", "version": "0.6" }`:
  - `drift/manifest.json` (uflowsd)
  - `microflows/runner/drift/manifest.json` — **two** entries, `microflows-runner`
    and `uflowsd`
  - `microflows/participant-stub/drift/manifest.json`
- **Four** `web-rest` pins across **three** generated lock files, all resolving to
  `0.6.5` with identical
  `sha256:7fbf466b95f16f3a3dae92df3f20fe4c1dc0a27f5b6f05e00280fa1cde8177b9`:
  - `drift/lock.json` — one entry (uflowsd)
  - `microflows/runner/drift/lock.json` — **two** entries
  - `microflows/participant-stub/drift/lock.json` — one entry

Mechanism: cert gates resolve dependencies via `drift lock emit --source-rebuild`,
which builds `web-rest` from drift-web **source** rather than the certified pkg
root. That source now produces only 0.7.0, so a `0.6` constraint has no candidate.
This is a version-constraint failure, not a compile or ABI failure — ABI 22 is
unchanged on both sides.

## Confirmed: our consumer surface is untouched by 0.7.0's breaking changes

drift-workflows uses exactly 16 `rest.*` symbols, across two files
(`microflows/runner/src/runner.drift`, `microflows/participant-stub/src/app.drift`):

    rest.Context      rest.Request     rest.Response    rest.add_middleware
    rest.add_route_group                rest.add_route_group_throws_route
    rest.add_throws_route               rest.bind        rest.body_json
    rest.build_app    rest.json_response                 rest.new_app_builder
    rest.path_param   rest.query_param rest.shutdown     rest.start

The two breaking changes in 0.7.0 do not intersect that set:

1. `with_response_header` now returns `core.Result<Void, HeaderError>`.
   **We never call it** (0 occurrences).
2. `RestError` gains a required, defaultless `headers_json: String`, so every
   construction site must supply it. **We never construct `RestError`**
   (0 occurrences tree-wide).

`json_response`, our only response constructor, keeps its signature verbatim in
0.7.0: `pub fn json_response(status: Int, body: String) nothrow -> Response`
(`web.source:packages/web-rest/src/response.drift:35`).

## Was Inferred, now CONFIRMED by build — zero source changes

The migration is a **constraint bump and relock with zero source changes**: move
four manifest declarations from `"0.6"` to `"0.7"` and re-emit the four locks.

**Established.** All three artifacts compile against `web-rest@0.7.0` on the run
toolchain (driftc 0.36.0 `b60b86dd`, ABI 22), and the only source edit in the
whole migration is `RUNNER_VERSION`. The original wording of this section is kept
below because the caveat it raised was the right one to raise — it just resolved
in the migration's favor. Response *framing*
changed in 0.7.0 (server-owned close, one `Connection` decision via the new
`DispatchOut`); we consume that behavior through `rest.start`/`rest.bind` rather
than by name, so a behavioral difference could surface at runtime even with an
unchanged call surface. The pool-framing fix means the old wire output was
malformed and clients were being lenient — our integration tests should be read
with that in mind rather than assumed stable.

That caveat was discharged by running the lane, not by assuming: 231/231 with the
four draining/503 cases green and no test edited. It still holds in reduced form —
the cert lane's own gates remain the authority on wire behavior.

## Checks against the reviewer's proposed step order (item 1 DISPROVED, 2-3 stood)

From `review_evidence` `9de49e96c377c4db41f68097b7ad37c8` (proposal: uflowsd
0.8.1, ranges to 0.7, regenerate 3 locks / reseal, focused compile + deploy).
Three checkable problems, found by reading the recipes rather than by running them:

1. ~~**`just reseal` cannot resolve 0.7.0 yet.**~~ **DISPROVED — this claim was
   wrong. See "Disproved hypothesis" below.** It asserted that web-rest 0.7.0
   existed nowhere as a published package and that drift-web had to stage it
   first. There was no producer prerequisite.
2. **`reseal` regenerates one lock, not three.** Its own completion line names
   `drift/manifest.json`, `drift/lock.json`, `drift/*.author-claim` — the root
   tree only. `microflows/runner/drift/lock.json` and
   `microflows/participant-stub/drift/lock.json` are separate component trees and
   need their own resolution. "Regenerate 3 locks / reseal" conflates the two.
3. **Manifest count: 3 files, 4 declarations.** `microflows/runner/drift/manifest.json`
   declares `web-rest 0.6` **twice** — once for `microflows-runner`, once for
   `uflowsd`. Editing per-file rather than per-declaration leaves one behind.

Confirmed and **not** a problem with the proposal: uflowsd 0.8.1 is the right
bump site and the only one. The root `drift/manifest.json` is the sole source of
truth for shipped versions (component manifests pin the sentinel `0.0.0`), and
uflowsd is the only artifact that depends on `web-rest` — `singular 0.10.0` and
`microflows 0.9.0` both depend on `mariadb-rpc` only.

## Two resolution paths must agree

The committed lock has to satisfy both: local `just reseal`, which resolves from
`DRIFT_PKG_ROOT`, and the cert gate, which resolves via
`drift lock emit --source-rebuild` from drift-web source. A lock that resolves
one way and not the other is the failure mode this finding started from.

## Decided (review 2026-08-11T20-29-58Z, P1) — sequencing is not an open question

**Adopt `web-rest 0.7` now, in this certification train.** `AGENTS.md` makes the
compatibility target current `main`, and a breaking `main` change immediate
integration work. The certification run *is* the staging overlay: it stages
web-rest 0.7.0 from drift-web `558b647` before it stages drift-workflows, so
waiting for 0.7.0 to certify first would deadlock this same combined train.

This closes both Open items. Falling back to certified 0.6.5, or introducing any
compatibility path, is explicitly out of bounds.

Relock runs against the **run's snapshot-gated 0.7.0 package inputs**, not against
`certified/current/pkgs`.

## Disproved hypothesis — "0.7.0 is published nowhere" (was wrong)

I claimed, twice and with confidence, that `web-rest 0.7.0` existed only as
drift-web source and that adoption was blocked on drift-web staging it. That was
**false**, and `workflows.reviewer` disproved it in `4aaf0177...`.

The error was a search-scope error, not a reasoning error downstream of bad data:
I searched only `/home/sl/opt/drift` and concluded "nowhere" from "not in the
place I looked." The package root the certification run uses lives outside that
tree:

    /home/sl/src/build-orchestrator/build/runs/20260811-193433-drift-lang-b60b86d/pkgs

It holds signed `web-rest/0.7.0` with source content id
`sha256:f59004d190744de7eda4151fb626d27fb06ab4c2835394dc8edfdc19af964071` in both
its cert-claim and author-claim, alongside every other dependency this repo needs
(`mariadb-rpc 0.8.2`, `mariadb-wire-proto 0.6.2`, `microflows 0.9.0`,
`net-tls 0.6.4`, `singular 0.10.0`, `web-client 0.5.5`, `web-jwt 0.5.4`).

Kept as history because the lesson generalizes: "absent from every root" is a
claim about search coverage, and it needs the coverage stated before it can carry
a blocker. The load-bearing negative should have been framed as "not in the roots
under `/home/sl/opt/drift`" — which is true, and would not have blocked anything.

## Confirmed (review P2): the justfile staging comment is stale — correct it here

The reviewer asked whether app staging is intentional. It is. The orchestrator's
own config, `build.source:orchestration.json:60`, gives drift-workflows:

    "stage_packages": ["{staged_drift}", "deploy", "--dest", "{pkgs_root}",
                       "--app-dest", "{apps_root}"]

That is **unfiltered** `drift deploy` — no `--artifact` selection — and it passes
`--app-dest`, so it deliberately stages the `kind:app` artifact. Corroborated by
`staged/apps/mariadb-failpoint-proxy` existing on this host.

The root `justfile` comment (the "Certification author/deploy surface" block)
claims the orchestrator "stages the PACKAGES ONLY:
`drift deploy --artifact singular --artifact microflows --dest <libs_root>`" and
that `uflowsd` is "NOT staged via the cert pool ... BY POLICY". Both clauses are
contradicted by the live config, which explains why the rejected run failed
precisely while staging that app. Per the review this is a comment correction in
this work, not a nested finding.

## Not a defect

Nothing here is a `CORE_BUG` for this repository. drift-web's 0.7.0 fixed a real
framing `CORE_BUG` on its side; the failure we see is the intended consequence of
a deliberate producer bump meeting a pinned consumer constraint. No workaround,
shim, or pin-pinning evasion should be introduced.

## Acceptance criteria — status

1. **MET.** `uflowsd` source-rebuild resolves `web-rest` with no unsatisfiable
   constraint. The reviewer independently ran `drift lock emit --source-rebuild`
   for all four artifact declarations against the failed-run snapshot with a
   candidate pool limited to snapshot-authorized upstream packages: all four
   exited 0, selected `web-rest@0.7.0`, and reported empty `added`, `removed`,
   `sha_drift`, `signer_drift` and `version_changed` evidence.
2. **OPEN.** Cert gates (test / stress / perf, normal + debug) — Slawomir's
   certification rerun, not run here. This is the only remaining criterion.
3. **MET.** Zero source changes were required by 0.7.0. The single source edit in
   the migration is `RUNNER_VERSION` `0.8.0` -> `0.8.1`, which the artifact
   version bump requires and 0.7.0 does not.

## Verified outcome

Built on the run toolchain (driftc 0.36.0 `b60b86dd`, ABI 22) against the run
package root:

- four `web-rest` pins moved `0.6.5` -> `0.7.0` across three lock files
  (root 1, runner 2, participant-stub 1); no unrelated pin moved in any file;
- `microflows-runner`, `uflowsd` and `microflows-participant-stub` all compile
  clean; `mfrunner` and `uflowsd` binaries both report `0.8.1`;
- uflowsd's build-info reads `driftc 0.36.0`, `git b60b86dd`, `abi 22`,
  `web-rest 0.7.0`;
- coordinator-singular integration 231/231, zero failures, no test edited,
  including `admission_draining_refuses_fresh_submission`,
  `admission_draining_resume_converges_pending_restart`,
  `service_draining_refuses_503`,
  `service_draining_resume_pending_restart_503`;
- `just reseal` and `just trust-check` green; `git diff --check` clean.

Per review `2026-08-11T20-58-13Z`, `drift/microflows.author-claim` and
`drift/singular.author-claim` were restored to their pre-work bytes — neither
artifact changed version, dependencies, or source content identity, so re-dating
those sealed releases was unrelated churn. Only `drift/uflowsd.author-claim`
stays re-minted, which is correct: that artifact did change.
