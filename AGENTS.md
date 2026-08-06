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

## Review findings tracking (work/finding-* subfolders)

This is the specialized workflow for review findings. If this repository has broader `work/<work-name>/` rules elsewhere, those continue to govern ordinary efforts; this section supersedes their file-naming and update rules only inside `work/finding-*`.

Every finding folder starts with `FINDING.md`, `PLAN.md`, and implementer-owned `PROGRESS.md`.

Review findings are tracked as dedicated subfolders under `work/`, one per finding: `work/finding-<slug>/`. Two goals: no finding discovered at any point is ever lost, and the agent and reviewer can work **concurrently** — while the agent is heads-down (or stuck) on finding-1, the reviewer keeps piling tests, evidence, repros, and proposed solutions into finding-2, finding-3, … without interrupting the work in flight.

**Process:**

- When a review surfaces a finding that is not being fixed on the spot, it gets its own `work/finding-<slug>/` folder capturing the finding (repro, suspected subsystem, evidence) at the time of discovery.
- Finding folders are a live drop-box: the reviewer may add material to any queued folder at any time. The agent does not need to react to those additions mid-task — queued folders are read when their turn comes.
- Use reviewer capacity to make queued findings implementation-ready, not merely identifiable. Before the implementer picks one up, the reviewer should add as much verified research as practical: a minimal repro and observed baseline; exact producer/consumer code paths and symbols; root-cause evidence with hypotheses clearly separated from facts; a recommended patch shape and affected-file boundary; semantic edge cases and interactions with current work; positive, negative, boundary, and compile/run regression cases; focused verification commands; and the applicable refactor-trigger result (or an explicit note that this repository has no trigger registry) for a defect. Prefer leaving the implementer a bounded execution/verification task over asking them to rediscover the defect. Research artifacts and proposed tests belong in the finding folder until implementation; do not edit implementer-owned `PROGRESS.md` or in-tree source/tests as part of reviewer-only research.
- Reviewer research is decision support, not an implementation specification. Label material by epistemic status where ambiguity is possible: **Observed** (reproduced evidence), **Confirmed** (code-path fact), **Inferred** (best current explanation), **Proposed** (one patch/test design), or **Open** (unresolved question). Use directive language only for repository/user contracts and verified acceptance criteria; phrase diagnoses and patch shapes as falsifiable claims or recommendations. The implementer must revalidate the current tree and is explicitly free to disprove, narrow, or replace the reviewer's diagnosis or proposal; record the contrary evidence and resulting decision in implementer-owned `PROGRESS.md` rather than following a reviewer theory that does not fit the code.
- Fallibility is symmetric: neither an implementer's `PROGRESS.md` claim nor a review report is authoritative merely because of its author or channel. The implementer independently checks reviewer evidence and assumptions; the reviewer independently checks implementation claims against the current diff, relevant code paths, repros, and regression coverage rather than accepting the status summary. Treat doubt, counterexamples, and corrections as required engineering inputs, not friction. Resolve disagreements with reproducible evidence and repository contracts, state remaining uncertainty plainly, and do not sign off while a material claim has only been asserted by the other role.
- Keep researching and enriching the next queued findings while implementation is the throughput bottleneck. Prioritize the next serial item first, then other queued items by likely implementation cost and risk. Re-check all captured evidence against the current tree when the item starts; implementation may have made earlier research stale.
- Findings are worked **serially** — one at a time, to completion (including the repository's applicable defect-completion criteria when relevant), before picking up the next.
- When picking up a finding, read the WHOLE folder fresh — it has likely grown since it was filed — and re-verify its claims against the current tree: earlier work may have resolved it in full or in part. Captured text going stale is expected; the folder is the tracking unit, not a living spec. If the finding is already fully resolved, record that outcome in the folder rather than re-fixing.
- Do not silently delete a finding folder because it looks stale — close it out explicitly (resolved by <what>, superseded by <what>, or fixed directly).
- Finding discovery is recursive and role-neutral: at any time while a current finding is being researched, reviewed, implemented, or verified, either the reviewer or the implementer may file another defect as a top-level `work/finding-<slug>/` or as a nested child finding. Use a child when the new defect is causally tied to or naturally scoped under the current finding and is expected to close with it; use a top-level finding when it is independent, separately schedulable, or may outlive the current parent. Filing the discovery immediately does not interrupt the serial-work rule—the new finding remains queued unless it is required to close the current finding's contract.
- When work on one finding uncovers a distinct child finding, use filesystem nesting rather than dot-notation names:
	```text
	work/finding-<parent-slug>/
	├── FINDING.md
	├── PLAN.md
	├── PROGRESS.md
	└── findings/
	    └── finding-<child-slug>/
	        ├── FINDING.md
	        ├── PLAN.md
	        └── PROGRESS.md
	```
	- Every child is a complete finding with its own `FINDING.md`, `PLAN.md`, and `PROGRESS.md`.
	- The parent `PROGRESS.md` lists each child and its status; the child `FINDING.md` names its parent and discovery context.
	- Do not use dotted top-level names or numeric prefixes for hierarchy/order; priorities change and the directory tree is the authority.
	- Keep nesting to at most two child levels. If a deeper child appears, or a child becomes independently scheduled or may outlive its parent, promote it to `work/finding-<child-slug>/` and update all live references; do not leave an alias/stub behind.
	- A parent cannot be deleted while it contains an open child finding. Close every child first or promote the open children before parent cleanup.
- Each review pass of a finding is journaled in that finding's root as `review-YYYY-MM-DDTHH-MM-SSZ.md`, using UTC (for example, `review-2026-08-03T14-00-33Z.md`). Reviews are append-only history: never edit or delete an earlier review file; the newest file is the review to answer. The implementer records its response/outcome and the current awaiting-review, changes-requested, or signed-off state in `PROGRESS.md`, leaving the review file itself untouched. Child findings use the same convention in their own root.
- Finding handoffs must follow `AGENTS-MAILBOX-PROTO.md`. Read that versioned protocol in full before publishing or consuming a handoff; it supersedes any older token convention in repository notes or finding artifacts.
- `PROGRESS.md` has a single writer: the implementer. The reviewer never updates it — reviewer input goes exclusively into `review-*.md` files and other evidence/repro material. One owner per channel keeps the status trail unambiguous: `PROGRESS.md` is always the implementer's claim of where things stand; `review-*.md` is always the reviewer's.
- Finding folders are **ephemeral**: they are deleted after the branch merges to main and the resolution is closed. No permanent or residual reference may point at them — not from code comments, source, tests, runners, or tools. Anything a finding produces that must outlive the branch (regression tests, doc updates, refactor-trigger entries) lands in the tree proper before the folder is closed, phrased so it stands alone without the folder.
