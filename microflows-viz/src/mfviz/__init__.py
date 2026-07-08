"""mfviz — the microflows-viz backend package (`microflows-viz serve`).

Slice 1 of work/viz-consolidation: a stdlib-HTTP backend that serves the existing
static UI plus a read-only JSON `/api/...` over the coordinator MariaDB. The DB
query logic is ported from microflows/tools/mfinspect (scheduled for removal once
this tool reaches parity).
"""
