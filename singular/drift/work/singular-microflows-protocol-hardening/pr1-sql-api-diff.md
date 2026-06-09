# PR1 — SQL / API diff (RETIRED — implemented)

**Status: IMPLEMENTED and retired.** This document was the pre-implementation diff sketch for PR1.
It has been retired because its detailed SQL/drift snippets showed shapes that were **superseded**
during review (`Duplicate`, the `Applied`/`Terminal` split, `renew()`, `ActiveInfo`, `WorkKind`,
parallel `response`/`error` columns, `INSERT IGNORE`) and would otherwise read as a competing contract.

- **Current contract:** [`plan.md`](plan.md) — including its **PR1 Decision Log** (the history of what
  changed and why, and the pending SQL sign-offs).
- **As-built code:** SQL in `singular/db/{schema,procs}/`; gateway in
  `singular/packages/singular/src/{gateway,lib}.drift`; tests in
  `singular/packages/singular/tests/` (incl. the isolated malformed-backend regression under
  `tests/fixtures/`, run via `just test-malformed-fixture`).
- **PR sequencing / remaining SQL proposals:** [`implementation-plan.md`](implementation-plan.md).

There is nothing to review here — see `plan.md`.
