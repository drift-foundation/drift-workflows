# Repo Agent Rules

## Drift context (required)

Primary docs (read first):

- Docs root: `https://github.com/drift-foundation/lang-toolchain/tree/main/docs`
- Language spec: `https://github.com/drift-foundation/lang-toolchain/tree/main/docs/design/drift-lang-spec.md`
- Stdlib spec: `https://github.com/drift-foundation/lang-toolchain/tree/main/docs/design/drift-stdlib-spec.md`
- Concurrency design: `https://github.com/drift-foundation/lang-toolchain/tree/main/docs/design/drift-concurrency.md`
- Tooling/packages: `https://github.com/drift-foundation/lang-toolchain/tree/main/docs/design/drift-tooling-and-packages.md`
- Effective guide: `https://github.com/drift-foundation/lang-toolchain/tree/main/docs/effective-drift.md`

Drift dependency policy:

- This repo tracks `drift-foundation/lang-toolchain` `main`.
- Compatibility target is current `main`, not historical snapshots.
- If a `main` change breaks this repo, treat it as immediate integration work.
- If breakage appears to be a Drift defect, follow the defect policy below and pin a minimal regression.

## Git usage (strict)

- Use `git` **only** for reviewing history or diffing (e.g. `git diff`, `git log`, `git show`, `git blame`).
- **Do not** stage or unstage changes (`git add`, `git restore --staged`, etc.) without explicit permission.
- **Do not** perform any mutating git operations without explicit permission (including `git commit`, `git merge`, `git rebase`, `git cherry-pick`, `git reset`, `git checkout/switch`, `git stash`, and tag/branch operations).
- **Do not** wrap long lines (calls with many arguments, long expressions) for readability; avoid indentation churn, especially if code is deeply nested.
- **Do not** edit existing tests without clear confirmation it is OK. Do not bend tests around defects.

## Announcements
- Read and publish cross-team announcements from/to /tmp/drift-announce/<iso-utc-datetime>-<repo>-release-notes.md

## Working notes / progress (required)

Every active effort lives under `work/<work-name>/` (see `work/README.md`). Each
such folder MUST carry two files:

- **`README.md`** — the durable charter: the plan, the bug/problem being solved,
  the work involved, and the end goal (objective, accepted design decisions,
  implementation plan, verification criteria, boundaries). It also holds the
  detailed per-round change log. This answers "what is this effort and why".
- **`Progress.md`** — the at-a-glance status: "where it stands + literal next
  action" (status/sub-step ledger, what landed, verification result, uncommitted
  worktree, next step). This answers "what is the current state".

- **Update `work/<work-name>/Progress.md` on every completed phase, step, or
  review-feedback round for that effort** — before considering the unit of work
  done. Treat the update as part of the task, not an afterthought.
- Each update reflects current reality: status/sub-step ledger, what landed,
  verification result (command + counts), the uncommitted worktree, and the
  literal next action. Keep it short and current; prune stale text.
- If an effort has no `Progress.md` yet, create one when you first touch it.

## Defect policy (strict)

- If behavior indicates a core defect (protocol parsing, state machine, concurrency, memory/lifetime, I/O correctness, or runtime integration), classify it immediately as `CORE_BUG`.
- Do not implement or retain workarounds for any confirmed or suspected compiler, runtime, toolchain, or other `CORE_BUG`.
- If the defect is external to this repository, block the dependent work until a certified root-cause fix is available.

### Regression-first requirement (mandatory)

For every suspected `CORE_BUG`, do this in order:

1. Add a minimal failing regression test (prefer e2e/integration when relevant, unit otherwise).
2. Confirm it fails on current behavior.
3. Fix the root cause.
4. Confirm regression passes.
5. Only then consider refactor/cleanup.

### No semantic masking

Forbidden:

- Rewriting control flow primarily to bypass correctness defects.
- Rewriting ownership/lifetime patterns primarily to hide memory/concurrency defects.
- Any source change whose main purpose is to avoid fixing root cause.
- Catch-all error handling, alternate APIs, compatibility shims, or representation changes introduced to avoid a confirmed or suspected compiler/runtime/toolchain defect.

### Stop-and-confirm gate

On first detection of a likely `CORE_BUG`, stop broader implementation changes and notify with:

- minimal repro
- failing test path
- suspected subsystem

Then continue only with the root-cause fix. If that fix belongs to an external project,
record the blocker and stop dependent implementation until the fixed certified dependency
is available.

### Completion criteria

A `CORE_BUG` is not done unless both are present:

- pinned regression test
- root-cause fix
