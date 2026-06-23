#!/usr/bin/env python3
"""Singular perf gate — run the built perf scenario, gate per-cycle cost vs a committed baseline.

The shared executor BUILDS the scenario (emit_test_plan.py perf); this harness RUNS it (DB-backed) and
gates. Metric: `per_cycle_us` for a fixed acquire->settle->inspect workload. Gating:
  - logical: scenario must exit 0 and report all CYCLES completed (the scenario enforces the count);
  - throughput: per_cycle_us must not exceed TOLERANCE x the committed, machine-keyed baseline — a
    cascading-slowdown guard that tolerates normal host variance (a strict wire-byte/packet baseline,
    the drift-mariadb-client convention, is a documented future deepening; we have no wire proxy yet).
A MISSING baseline HARD-FAILS (never auto-recorded during a gate — that would let a fresh cert host
self-baseline and skip the regression check); only an explicit `--update-baseline` records one, which is
then committed. Baselines (perf/baselines/<machine>.json) are committed; results/ are gitignored.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # singular/drift
BASELINE_DIR = ROOT / "perf" / "baselines"
RESULTS_DIR = ROOT / "perf" / "results"
GATED = "per_cycle_us"
TOLERANCE = 3.0


def machine_id() -> str:
    p = Path("/etc/machine-id")
    raw = p.read_text().strip() if p.exists() else "unknown"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bin-dir", required=True, help="dir holding the built `singular-perf` binary")
    ap.add_argument("--update-baseline", action="store_true",
                    help="overwrite this machine's baseline with the current run")
    args = ap.parse_args()

    binp = Path(args.bin_dir) / "singular-perf"
    if not binp.exists():
        sys.exit(f"error: perf binary not found at {binp}")
    out = subprocess.run([str(binp)], capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        sys.exit(f"error: perf scenario exited {out.returncode} (logical failure)")

    metric = None
    for line in out.stdout.splitlines():
        s = line.strip()
        if s.startswith("{") and "scenario" in s:
            metric = json.loads(s)
            break
    if not metric:
        sys.exit(f"error: no perf metric line in scenario output:\n{out.stdout}")
    print(f"[perf] {metric}")

    mid = machine_id()
    bfile = BASELINE_DIR / f"{mid}.json"
    name, val = metric["scenario"], metric[GATED]

    base = json.loads(bfile.read_text()) if bfile.exists() else {"machine_id": mid, "scenarios": {}}
    recorded = base.get("scenarios", {}).get(name, {}).get(GATED)

    if args.update_baseline:
        # DELIBERATE baseline (re)record — the ONLY path that writes a baseline. Commit the result.
        base.setdefault("scenarios", {})[name] = {GATED: val}
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        bfile.write_text(json.dumps(base, indent=2) + "\n")
        print(f"[perf] recorded baseline {name}.{GATED}={val} for machine {mid} "
              f"(commit perf/baselines/{mid}.json)")
        gate_ok, detail = True, "baseline recorded"
    elif recorded is None:
        # HARD FAIL: a normal gate run with NO committed baseline must NOT silently pass (a fresh cert
        # host would otherwise self-baseline and skip the regression check). Baselines are recorded
        # deliberately + committed; never minted during a gate run.
        sys.exit(
            f"error: no committed perf baseline for scenario '{name}' on machine {mid}.\n"
            f"  expected: perf/baselines/{mid}.json with scenarios.{name}.{GATED}\n"
            f"  fix: run `just perf --update-baseline` on THIS cert host and COMMIT the new baseline.\n"
            f"  the gate refuses to pass without a committed baseline to compare against.")
    else:
        limit = recorded * TOLERANCE
        gate_ok = val <= limit
        detail = f"{name}.{GATED}={val} baseline={recorded} limit={limit:.0f} -> {'PASS' if gate_ok else 'FAIL'}"
        print(f"[perf] {detail}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(
        json.dumps({"machine_id": mid, "metric": metric}, indent=2) + "\n")

    if not gate_ok:
        sys.exit(f"error: perf regression — {detail}")
    print("[perf] OK")


if __name__ == "__main__":
    main()
