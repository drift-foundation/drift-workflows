#!/usr/bin/env python3
"""Export a REAL workflow's event log as a demo tape — proof the animation is driven by the coordinator,
not a mock.

Reads `tb_mf_workflow_event` for one workflow_id and emits a scenarios.js-compatible tape (best-effort
mapping from the durable audit `kind` to the lifecycle-machine event). Add the printed `events: [...]`
block to scenarios.js (or just eyeball the raw timeline it prints).

    python3 export_events.py --workflow-id <hex32> [--host 127.0.0.1 --port 3306 \
        --user root --password '' --database microflows]

Uses the `mariadb`/`mysql` CLI (no pip deps) — the same client the integration harness uses.
"""
import argparse, json, shutil, subprocess, sys

# durable audit kind  ->  lifecycle-machine event (see microflows.machine.js / scenarios.js)
KIND_TO_EVENT = {
    "operation_requested": "DISPATCH",
    "operation_settled": "SETTLED",        # promoted to SETTLED_FINAL if followed by workflow_completed
    "workflow_completed": "SETTLED_FINAL",
    "operation_failed": "REJECTED",        # (an authored `fail` also lands here; same transition)
    "reversal_begun": None,                # implicit in REJECTED/FAIL -> reversing
    "compensation_requested": None,        # paired with compensation_settled
    "compensation_settled": "COMPENSATED",
    "compensation_blocked": "COMP_REJECTED",
    "participant_route_404": "ROUTE_404",
    "participant_route_unknown": "EXHAUSTED",
    "failed": "ALL_COMPENSATED",           # terminal; reversed(5)/failed(7) both render {workflow:failed}
}


def _client():
    for c in ("mariadb", "mysql"):
        if shutil.which(c):
            return c
    sys.exit("error: need the `mariadb` or `mysql` CLI on PATH")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow-id", required=True, help="32-hex-char workflow id (no dashes)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", default="3306")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--database", default="microflows")
    a = ap.parse_args()

    sql = (
        "SELECT DATE_FORMAT(event_ts,'%Y-%m-%dT%H:%i:%s.%f'), kind, payload "
        f"FROM tb_mf_workflow_event WHERE workflow_id = UNHEX('{a.workflow_id}') ORDER BY event_ts"
    )
    cmd = [_client(), "-h", a.host, "-P", str(a.port), "-u", a.user]
    if a.password:
        cmd.append(f"-p{a.password}")
    cmd += ["-N", "-B", "-e", sql, a.database]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"query failed: {out.stderr.strip()}")

    rows = [ln.split("\t") for ln in out.stdout.strip().splitlines() if ln.strip()]
    if not rows:
        sys.exit(f"no events for workflow {a.workflow_id} (is it the right id / database?)")

    print(f"\n--- REAL event log for {a.workflow_id} ({len(rows)} events) ---")
    for ts, kind, payload in rows:
        print(f"  {ts}  {kind:<26} {payload}")

    events, n = [], len(rows)
    for i, (ts, kind, payload) in enumerate(rows):
        ev = KIND_TO_EVENT.get(kind)
        if ev is None:
            continue
        if ev == "SETTLED" and i + 1 < n and rows[i + 1][1] == "workflow_completed":
            ev = "SETTLED_FINAL"
        try:
            p = json.loads(payload)
        except Exception:
            p = {}
        item = {"type": ev, "kind": kind, "note": f"seq {seq}: {kind}"}
        if "reason" in p:
            item["reason"] = p["reason"]
        events.append(item)

    print("\n--- scenarios.js tape (paste into scenarios.js) ---")
    print("    events: [")
    for e in events:
        print("      " + json.dumps(e) + ",")
    print("    ],")


if __name__ == "__main__":
    main()
