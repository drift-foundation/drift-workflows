# Workflow Composition — Progress

## Status

Scheduled, not started. This is the next planned design effort after the current microflows release is cut and handed back to the app team.

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

This note only adds `work/workflow-composition/README.md` and `work/workflow-composition/PROGRESS.md`.

## Literal Next Action

After the release cut, review the charter with K and expand it into a detailed design: syntax, IR, durable state transitions, compensation behavior, and test plan.
