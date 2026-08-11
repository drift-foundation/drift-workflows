# Plan — cert web-rest 0.7 adoption

Revised after reviews `2026-08-11T20-29-58Z` and `2026-08-11T20-58-13Z`.
**Steps 1-9 are DONE and verified; step 10 (certification rerun) is the only work
left, and it is Slawomir's gate.**

All execution used the run package root and its sibling toolchain:

    DRIFT_PKG_ROOT=.../build/runs/20260811-193433-drift-lang-b60b86d/pkgs
    DRIFT_TOOLCHAIN_ROOT=.../build/runs/20260811-193433-drift-lang-b60b86d/toolchain

An earlier revision of this plan carried a step 2b asserting that drift-web had to
publish 0.7.0 first. That was **disproved** — a search-scope error on my part; the
package was already signed and present in the run root. See "Disproved hypothesis"
in `FINDING.md`.

## 1. Independent inspection — DONE

Producer and consumer evidence gathered without relying on the reviewer's
diagnosis; recorded in `FINDING.md`. Version-constraint failure; consumer symbol
surface unaffected by 0.7.0's two breaking changes.

## 2. Sequencing — DECIDED, no longer a blocker

Adopt `web-rest 0.7` now. No fallback to certified 0.6.5, no compatibility path.

## 3. Bump the constraint — three files, FOUR declarations — DONE

    workflows.source:drift/manifest.json                              1
    workflows.source:microflows/runner/drift/manifest.json            2
    workflows.source:microflows/participant-stub/drift/manifest.json  1

`"version": "0.6"` -> `"version": "0.7"`. Drive this off the declaration count,
not the file count — `runner/drift/manifest.json` carries two.

## 4. Release-integrity work (review P1 — was missing from the first plan) — DONE

Changing the root dependency declaration changes uflowsd's source content
identity, so the sealed `0.8.0` cannot be re-minted with different manifest
content:

- root `drift/manifest.json`: `uflowsd` `0.8.0` -> `0.8.1`. Sole version-
  authoritative site; component manifests keep the `0.0.0` sentinel.
- `microflows/runner/src/runner.drift:54`: `RUNNER_VERSION` `"0.8.0"` -> `"0.8.1"`.
  It backs `--version` for both `mfrunner` and `uflowsd`, so it must not drift
  from the manifest.
- `microflows/CHANGELOG.md`: a self-contained entry explaining the 0.7 adoption —
  written to stand alone after this ephemeral folder is deleted.
- regenerate `drift/uflowsd.author-claim` and the root lock through the normal
  reseal path.

Patch class is appropriate **only if** step 6 confirms no workflows-owned
contract change. If it finds one, re-evaluate the version class rather than
forcing the patch bump.

## 5. Relock — four pins across three files — DONE

    drift/lock.json                              1 entry
    microflows/runner/drift/lock.json            2 entries
    microflows/participant-stub/drift/lock.json  1 entry

`just reseal` regenerates the **root** lock only; the two component trees resolve
separately. Relock against the run's snapshot-gated 0.7.0 package inputs; the
certified root cannot serve this, since it carries `web-rest 0.6.5` only.

Result: all four pins moved `0.6.5` -> `0.7.0` and **no unrelated pin moved** in
any of the three files.

Expect the `web-rest` version, `sha256` and `source_content_id` to move in all
four pins and nothing else to move. **If an unrelated dependency pin moves,
stop** — that is a signal to investigate, not a relock to accept.

## 6. Compile and prove the zero-source-change claim — DONE

Build `uflowsd`, `microflows-runner`, and participant-stub. Expect zero migration
source edits beyond `RUNNER_VERSION`. The `FINDING.md` inference is established
only here; any compile error is evidence against it and gets recorded and
root-caused, never patched around.

## 7. Focused service integration on 0.7.0 — DONE (231/231)

Cover the existing 503/draining and connection behavior. 0.7.0 changed response
framing and connection ownership, and `pool_framing_test` had been passing on
client leniency while the wire was malformed — so a previously green suite is not
by itself evidence. Do not edit an existing test to accommodate a failure; a
correctness symptom trips the repository's `CORE_BUG` stop gate.

## 8. Trust / reseal checks — DONE

Root trust check passes and the author claim matches the bumped manifest.

## 9. Correct the stale staging comment (review P2) — DONE

App staging is intentional per `build.source:orchestration.json:60`. Fix the
root `justfile` "Certification author/deploy surface" comment, which still claims
packages-only staging and "uflowsd NOT staged via the cert pool BY POLICY". No
nested finding needed.

## 10. Certification rerun and close-out — THE ONLY REMAINING WORK

Slawomir's certification rerun is the authority and the terminal gate; it is not
run from here. Per review `2026-08-11T20-58-13Z`, the local repository
stress/perf sweep is **not** required before re-review — the rerun is the
authority, and machine-capacity guidance asked teams to avoid redundant heavy
gates.

On a passing rerun: land anything durable in the tree proper (the `CHANGELOG.md`
entry is already written to stand alone) and close this ephemeral folder out
explicitly.
