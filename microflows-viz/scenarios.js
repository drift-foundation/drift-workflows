// Four event tapes for the Microflows lifecycle demo. Each event drives the machine; `kind` is the
// REAL `tb_mf_workflow_event` audit kind the coordinator writes, `note` is the narration line.
// These mirror real integration-suite runs (coordinator-singular) — see export_events.py to replay
// an actual workflow_id instead.

export const scenarios = {
  happy: {
    title: 'Happy path — reserve → charge → completed',
    blurb: 'A two-step workflow runs clean to a success terminal.',
    events: [
      { type: 'DISPATCH', op: 'reserve', kind: 'operation_requested', note: 'PUT reserve → participant (stable operation_id)' },
      { type: 'SETTLED', kind: 'operation_settled', note: '200 {reserved} → durable checkpoint #1' },
      { type: 'DISPATCH', op: 'charge', kind: 'operation_requested', note: 'PUT charge → participant' },
      { type: 'SETTLED_FINAL', kind: 'workflow_completed', note: '200 {charged}, final node → completed (exit 0)' },
    ],
  },

  compensate: {
    title: 'Reject → compensation — charge fails, reserve is released',
    blurb: 'A definite failure unwinds the prior checkpoint in reverse (the saga model). No code is written for the undo — the compensation binding drives it.',
    events: [
      { type: 'DISPATCH', op: 'reserve', kind: 'operation_requested', note: 'PUT reserve → participant' },
      { type: 'SETTLED', kind: 'operation_settled', note: '200 {reserved} → checkpoint #1 (compensable)' },
      { type: 'DISPATCH', op: 'charge', kind: 'operation_requested', note: 'PUT charge → participant' },
      { type: 'REJECTED', reason: 'charge_declined', kind: 'operation_failed', note: '400 → definite forward failure → begin reversal' },
      { type: 'COMPENSATED', kind: 'compensation_settled', note: 'PUT release(reservation_id) → 200 — reserve undone' },
      { type: 'ALL_COMPENSATED', kind: 'failed', note: 'stack empty → reversed = {failed, compensated:true} (exit 3)' },
    ],
  },

  route404: {
    title: 'Persistent route-404 → blocked (the durable reconcile budget) ★',
    blurb: 'A participant goes dark. The coordinator retries within a DURABLE, bounded budget, then parks the workflow for an operator — instead of hanging forever or corrupting state.',
    events: [
      { type: 'DISPATCH', op: 'reserve', kind: 'operation_requested', note: 'PUT reserve → participant' },
      { type: 'ROUTE_404', reason: 'participant_route_404', kind: 'participant_route_404', note: '404 (no record) + re-PUT 404 → confirmed route-unknown · budget attempt 1' },
      { type: 'DEFER', kind: '(deferred)', note: 'within budget → release lease, retry later (resume re-reads the SAME row)' },
      { type: 'ROUTE_404', reason: 'participant_route_404', kind: 'participant_route_404', note: 'still 404 → budget attempt 2' },
      { type: 'EXHAUSTED', reason: 'participant_route_unknown', kind: 'participant_route_unknown', note: 'wall-time budget spent → blocked(forward), disposition indeterminate' },
      { type: 'OPERATOR_RESOLVE', kind: '(operator action)', note: 'an operator resolves the block → resolved_exception' },
    ],
  },

  authoredFail: {
    title: 'Result branch + authored fail — case result → fail "payment_declined"',
    blurb: 'Business policy lives in the .mf: a participant 200 decline is a normal RESULT; the workflow branches on it and authors its own failure, which still unwinds.',
    events: [
      { type: 'DISPATCH', op: 'authorize', kind: 'operation_requested', note: 'PUT authorize → participant' },
      { type: 'SETTLED', kind: 'operation_settled', note: '200 {status:"declined"} → checkpoint (a normal result, not an error)' },
      { type: 'FAIL', reason: 'payment_declined', kind: 'operation_failed', note: 'case result auth.status { "declined" { fail "payment_declined" } } → begin reversal' },
      { type: 'COMPENSATED', kind: 'compensation_settled', note: 'PUT undo-authorize → 200' },
      { type: 'ALL_COMPENSATED', kind: 'failed', note: 'stack empty → reversed = {failed, reason:"payment_declined", compensated:true}' },
    ],
  },
};
