# Workflow Composition — Progress

## Status

Release is cut + announced. Design at `DESIGN.md` is at **rev 2** (K rounds 1–2 folded in). **Decision #1
(transport) is RESOLVED → internal durable host API, async + awaited** — a workflow call is a pending
**call operation**; the parent stays in normal forward execution, and a blocked child does **not** cascade
up the call tree. Terminology fixed (workflow call / child workflow / call operation; avoid "callback").
Still OPEN and gating the slice: **decision #2 (compensation mode + whether T1 reverse-child reopen is in
slice 1)** and the **liveness/stuck-child policy** for the internal path.

## Current Scope

High-level charter only:

- any workflow step may be a child workflow;
- child workflow owns its own durable state;
- parent treats the child call as one forward step/checkpoint with result data flow;
- compensations may be workflows;
- dynamic fan-out uses stable child workflow ids, not operation occurrence indexes.

## Verification

None yet. No code changes are part of this effort.

## Dirty Worktree

This effort adds `work/workflow-composition/README.md`, `PROGRESS.md`, and `DESIGN.md` (the revised first design pass). No code changes.

## Literal Next Action

With decision #1 settled (internal host API), the next design step is to **decide the compensation mode and
the liveness policy BEFORE naming slice 1** — do not pre-commit to reverse-child. Specifically: (a) decision
#2 — reverse-child-by-default (which pulls **T1: `completed→reversing` reopen** into slice 1) vs ship
compensating-workflow / no-comp first (slice 1 avoids T1, stays small); (b) the stuck-child budget semantics
(push-vs-poll, and whether a blocked child is excluded from the budget). Only then name + build slice 1
(single non-fan-out call, spine + recursion guard + one compensation mode).
