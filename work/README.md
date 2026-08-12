# work/ — per-effort working notes

A lightweight convention so restart context is **explicit**, not reconstructed
from conversation history and a dirty worktree.

- `work/*` holds ONLY **active or scheduled** efforts: one folder per effort,
  `work/<short-kebab-name>/`.
- When an effort LANDS, **delete its folder** — the commit history is the record
  (the working notes are scaffolding, not an archive). Do not leave landed efforts
  as a log; that just duplicates history and goes stale.
- Never delete a work folder before its record-bearing commit has been pushed. Every review-finding folder must be committed and pushed at least once for backup, even when it is included only "for the record" or is unrelated to the commit's main change; after the finding is closed, remove it in a follow-up cleanup commit and push that commit.
- Each folder has a `README.md` (the charter — sections below) and a `PROGRESS.md`
  (at-a-glance status + literal next action). Keep both short and current.
- These are working notes, not design docs. Authoritative architecture lives in
  `microflows/doc/` (e.g. `microflows_design.md`, `security_model.md`); durable
  facts go to commit messages.

Each effort `README.md` records:

- **Short-term objective** — the one thing this effort delivers.
- **Current behavior / problem** — what's wrong or missing today.
- **Accepted design decisions** — choices already settled (and why).
- **Concrete implementation plan** — ordered, checkable steps.
- **Files likely affected** — the blast radius.
- **Verification criteria** — how we know it's done (commands + expected result).
- **Current status and next action** — where it stands; the literal next step.
- **Open questions / blockers** — anything unresolved.
- **Relevant review findings** — the review items this effort answers.
