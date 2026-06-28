# Workflow Composition — Charter

## Short-Term Objective

After the current microflows release is cut, design a first-class model where any workflow step can be another workflow. Compensating actions can also be workflows, but compensation is only one use case of the broader workflow-composition model.

This effort is conceptual for now. Detailed design and implementation sequencing will be worked through with K after the release handoff.

## Current Behavior / Problem

Microflows currently preserves a simple and important invariant: a participant operation node executes at most once per workflow instance. Operation identity is node-addressed, and reversal relies on a one-to-one relationship between a settled checkpoint and its compensation.

That model intentionally does not support repeated side-effect operations inside loops or dynamic fan-out inside one workflow instance. If a business process needs to perform many side effects, or if the undo action is more complex than one participant call, the current answer is to hide that complexity behind one participant operation.

That is workable for v1, but not sufficient long term. Some domains need ordinary forward steps to be child workflows:

- one parent workflow starts many child workflows, one per stable item key;
- a parent workflow waits for child outcomes and applies a policy;
- a complex business step is implemented as a reusable workflow rather than one participant operation;
- a parent workflow delegates a sub-process to another team-owned workflow;
- a parent reversal drives child compensation;
- a compensation action can itself be a workflow with multiple steps, retries, blocking, and its own compensation model.

## Accepted Design Direction

- Preserve the current "operation node executes at most once" invariant.
- Do not add occurrence indexes to participant operation identity.
- Do not permit ordinary participant side effects inside runtime loops.
- Model a child workflow call as a normal parent step that can settle, checkpoint, and feed results forward like any other operation.
- The child workflow owns its own durable state, checkpoints, retries, blocks, failures, and compensation.
- The parent records and compensates the child call as one checkpoint, using a stable child workflow id.
- Dynamic fan-out should be child-workflow keyed, not occurrence keyed: `child_workflow_id = H(parent_workflow_id, node_id, item_key)`.
- A compensating workflow is a valid compensation target, but it should run as a workflow with its own identity and state rather than being inlined into the parent's checkpoint stack.

## High-Level Model

Treat "call workflow" as a step kind with participant-shaped lifecycle semantics from the parent coordinator's point of view:

- start or reassert a child workflow under a stable operation id;
- poll/inspect the child until it produces a terminal outcome document;
- expose the child result to later parent steps;
- map child `completed`, `failed`, or `blocked` into parent-authored control flow;
- on parent reversal, invoke the declared child-compensation behavior.

The key rule: the parent checkpoints "child workflow X reached outcome Y"; it does not absorb the child's internal checkpoints.

## Concrete Implementation Plan

Not yet designed. Expected design phases:

1. Define the neutral contract for a workflow-call step.
2. Define child workflow id derivation and idempotent reassert semantics.
3. Define how the parent awaits child terminal outcomes.
4. Define how normal parent data flow consumes child workflow results.
5. Define how parent compensation drives child compensation or a separate compensating workflow.
6. Define fan-out over stable item keys without occurrence indexes.
7. Define blocked/failed propagation policy from child to parent.
8. Identify grammar, IR, host, runner, schema, and docs changes.
9. Add focused tests for recovery, replay, result data flow, compensation ordering, and blocked child workflows.

## Files Likely Affected

Likely, but not committed to until design:

- `microflows/runner/src/parser.drift`
- `microflows/runner/src/ir.drift`
- `microflows/runner/src/runner.drift`
- `microflows/packages/microflows/src/host.drift`
- `microflows/db/`
- `microflows/doc/microflows_design.md`
- `microflows/doc/uflowsd_participant_contract.md`
- `microflows/doc/microflows_user_guide.md`
- integration tests under `integration/coordinator-singular/`

## Verification Criteria

To be defined with the detailed design. Expected minimum:

- child workflow call is idempotent across retry and resume;
- a child workflow can be used as an ordinary forward step, not only as compensation;
- child workflow results can feed later parent steps;
- parent terminal replay does not require a live child participant/service;
- parent reversal invokes child compensation exactly once;
- child blocked state renders coherently through the parent;
- dynamic fan-out uses stable item keys and never occurrence indexes;
- full root `just test` green.

## Current Status And Next Action

Status: scheduled for after the current microflows release cut.

Next action: after handoff, discuss with K and turn this charter into a concrete design plan with syntax, durable state transitions, and test cases.

## Open Questions

- Is workflow composition a language-level construct, a standard participant adapter, or both?
- Does parent `.mf` see the full child outcome document, or a narrowed result type?
- How does a parent express policy for child `blocked` vs child `failed`?
- Is child compensation always "reverse the child", or can a parent declare a distinct compensating workflow?
- How are child workflow ids derived when fan-out input comes from a prior operation result?
- What operator-resolution path is needed for a blocked child inside a blocked parent?

## Relevant Review Findings

This effort follows from the current release's operation-identity decision: no occurrence indexes and no repeated side-effect operation nodes. Workflow-to-workflow calls are the planned escape hatch for dynamic fan-out and complex compensations.
