# work/ — per-effort working notes

A lightweight convention so restart context is **explicit**, not reconstructed
from conversation history and a dirty worktree.

- One folder per active effort: `work/<short-kebab-name>/`.
- Each folder has a `README.md` capturing the sections below.
- Keep it short and current. When an effort lands, set its status to **Done**
  (with the landing commit) and either delete the folder or leave it as a record.
- These are working notes, not design docs. Authoritative design lives in
  `microflows/doc/microflows_design.md`; durable facts go to commit messages.

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
