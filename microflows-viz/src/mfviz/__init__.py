"""mfviz — the microflows-viz backend package (`microflows-viz serve`).

The stdlib-HTTP backend (work/viz-consolidation) serving the operator UI plus a
read-only JSON `/api/...` over the coordinator MariaDB. microflows-viz is the
successor to (and sole replacement for) the retired mfinspect CLI: its query
logic originated there and its responses are pinned by fixture-owned golden
tests minted at mfinspect's retirement (tests/test_golden.py).
"""
