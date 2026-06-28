// Microflows workflow lifecycle — XState v5 model for the Stately visualizer/inspector.
//
// This is a FAITHFUL model of the durable coordinator state machine: the top-level states are the
// seven durable workflow states (state.drift), and the events are the real audit kinds the
// coordinator appends to `tb_mf_workflow_event` (operation_requested / operation_settled /
// operation_failed / reversal_begun / compensation_settled / participant_route_404 /
// participant_route_unknown / workflow_completed / failed). It is driven by the event tapes in
// scenarios.js, or by a REAL run exported with export_events.py.
//
// View it three ways (see README.md):
//   1. open index.html       -> live animated playback in the Stately inspector
//   2. paste into stately.ai/editor ("Edit as code") -> clean diagram + click-to-simulate
//   3. import into your own XState app
import { setup, assign } from 'https://esm.sh/xstate@5';

export const microflowsMachine = setup({
  actions: {
    countAttempt: assign({ attempts: ({ context }) => context.attempts + 1 }),
    resetAttempts: assign({ attempts: 0 }),
    setOp: assign({ op: ({ event }) => event.op ?? '' }),
    setReason: assign({ reason: ({ event }) => event.reason ?? '' }),
  },
}).createMachine({
  id: 'microflows',
  description:
    'A Microflows workflow instance. Durable state lives in MariaDB; the runner is a stateless one-shot ' +
    'driver. Every transition here is a fenced, atomic stored-procedure commit — recovery re-drives from ' +
    'the durable state, never from runner memory.',
  context: { attempts: 0, op: '', reason: '' },
  initial: 'forward',
  states: {
    // ---- state 1 -------------------------------------------------------------------------------
    forward: {
      description: 'Driving the plan forward, one durable operation at a time (effectively-once).',
      initial: 'driving',
      states: {
        driving: {
          description: 'Advance the pure control flow (if/case/merge) and pick the next operation node.',
          on: {
            DISPATCH: { target: 'awaiting', actions: 'setOp' },              // -> operation_requested
            FAIL: { target: '#microflows.reversing', actions: 'setReason' }, // a `fail` node reached while advancing
            COMPLETE: '#microflows.completed',                               // a return node with no further op
          },
        },
        awaiting: {
          description: 'PUT to the participant under a stable operation_id; await a settled result.',
          on: {
            SETTLED: { target: 'driving', actions: 'resetAttempts' },   // operation_settled (more steps)
            SETTLED_FINAL: '#microflows.completed',                     // operation_settled (final)
            REJECTED: { target: '#microflows.reversing', actions: 'setReason' }, // operation_failed (had checkpoints)
            REJECTED_FIRST: { target: '#microflows.failed', actions: 'setReason' }, // operation_failed (nothing to unwind)
            FAIL: { target: '#microflows.reversing', actions: 'setReason' },        // authored `fail "<reason>"`
            ROUTE_404: { target: 'reconciling', actions: 'countAttempt' },          // participant_route_404
          },
        },
        reconciling: {
          description:
            'Participant has NO record (a confirmed route-404). Advance the DURABLE reconcile budget ' +
            'on the operation row — a resume can never reset it.',
          on: {
            DEFER: { target: 'awaiting', description: 'Within budget -> defer + retry later.' },
            ROUTE_404: { actions: 'countAttempt', description: 'Still no record -> another attempt.' },
            EXHAUSTED: { target: '#microflows.blocked', actions: 'setReason' }, // participant_route_unknown
          },
        },
      },
    },
    // ---- state 2 -------------------------------------------------------------------------------
    reversing: {
      description: 'A definite failure: unwind the settled checkpoints in REVERSE order (saga compensation).',
      initial: 'compensating',
      states: {
        compensating: {
          description:
            'Compensate the top checkpoint. The reverse op receives the forward {input, result} envelope ' +
            '(e.g. the reservation_id it must void).',
          on: {
            COMPENSATED: { target: 'compensating', description: 'compensation_settled -> pop to the next checkpoint.' },
            ALL_COMPENSATED: '#microflows.reversed',                              // stack empty
            COMP_ROUTE_404: { target: 'comp_reconciling', actions: 'countAttempt' },
            COMP_REJECTED: { target: '#microflows.blocked', actions: 'setReason' }, // compensation_blocked
          },
        },
        comp_reconciling: {
          description: 'Route-404 on the compensation. Advance the REVERSE reconcile budget (on the checkpoint row).',
          on: {
            DEFER: { target: 'compensating', description: 'Within budget -> defer + retry.' },
            EXHAUSTED: { target: '#microflows.blocked', actions: 'setReason' },
          },
        },
      },
    },
    // ---- state 3 -------------------------------------------------------------------------------
    blocked: {
      description:
        'blocked_resolution: automatic execution cannot proceed (e.g. a persistent participant ' +
        'route-404). Parked for an operator. NOT failed, NOT reversed, never a silent infinite pending.',
      on: { OPERATOR_RESOLVE: '#microflows.resolved_exception' },
    },
    // ---- terminal states 4 / 5 / 6 / 7 ---------------------------------------------------------
    completed: { type: 'final', description: 'state 4 — success terminal. {workflow:completed} (exit 0).' },
    reversed: { type: 'final', description: 'state 5 — a real unwind ran. {workflow:failed, compensated:true} (exit 3).' },
    resolved_exception: { type: 'final', description: 'state 6 — an operator resolved the block.' },
    failed: { type: 'final', description: 'state 7 — definite failure, nothing to unwind. {workflow:failed, compensated:false} (exit 3).' },
  },
});
