#!/usr/bin/env python3
"""Emit a drift_test_run.py plan for a Microflows gate or a single dev test.

Microflows's PLAN EMITTER — the small per-project POLICY piece; the shared
toolchain executor (`$DRIFT_TOOLCHAIN_ROOT/lib/tools/drift_test_run.py`) owns
the mechanism (parallel compile under the flocker pool, run scheduling, dedup,
valgrind wrap, heartbeat, host concurrency budget). Mirrors the Singular
emitter's structure (see ../../singular/drift/tools/emit_test_plan.py).

Gates:
  test  — unit + e2e, each built {base, asan}. Unit runs base/memcheck/asan in
          PARALLEL (DB-free). e2e runs base/asan/memcheck SERIALIZED on the
          shared MariaDB instance via one `mode:serial group:<DB_GROUP>` (the
          key is the *instance*, not the database; resource contention, not
          state).
Dev:
  one --file F     — build + run one test (base), for fast iteration.
  compile --file F — type-check one file against microflows's sources (no run).

Fixture isolation: each e2e run-job gets a per-job env MICROFLOWS_E2E_NS =
drift-test-<nonce>-<lane>. Tests derive workflow IDs / record paths from this
namespace so base/asan/memcheck lanes (and repeated invocations) never collide
on durable rows.

Naming: every build `out` is `microflows-<leaf>-<stem>#<variant>`, DOT-FREE.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "drift" / "manifest.json"
LOCK = ROOT / "drift" / "lock.json"
TWB = "64"
ARTIFACT = "microflows"

# Foundation-libs root for the external mariadb deps.
PKG_ROOT = os.path.abspath(os.environ.get(
    "DRIFT_PKG_ROOT", os.path.expanduser("~/opt/drift/certified/current/pkgs")))

# Host-global mutex key naming the shared MariaDB *instance* (mdb114-a @ :34214).
# Must match other consumers' string (singular, mariadb-client) to serialize
# across suites on the one physical box. This DEFAULT serializes a direct executor
# run on the shared resource. An orchestrator that already holds the shared lock
# across a wider setup+DB-test phase passes a non-shared key via the `--db-group`
# CLI flag (a CONTROLLED arg from the locked recipe — never an ambient env var),
# so the executor's per-job lock can't deadlock against that outer hold (flocker
# is not re-entrant).
DB_GROUP = "mariadb-mdb114-a"

# DB-free unit tests (globbed for executable `fn main` entries).
UNIT_ROOT = "packages/microflows/tests/unit"
# DB-backed e2e tests — curated + ordered (order is significant: serial on the DB).
LIVE_TESTS = [
    "packages/microflows/tests/e2e/live_lease_test.drift",
    "packages/microflows/tests/e2e/live_reversal_test.drift",
    "packages/microflows/tests/e2e/live_call_test.drift",
    "packages/microflows/tests/e2e/live_args_test.drift",
]

# Per-invocation nonce → fresh fixture namespace each `just test`.
NONCE = f"{os.getpid()}-{time.time_ns()}"


def resolved_dep_flags():
    """Full resolved dep set (incl. transitive) for `microflows` via the repo-root
    tools/cert_deps.py authority: committed lock in the strict dev lane;
    snapshot-gated source-rebuild resolution under DRIFT_CERT_MODE=certify
    (lock demoted to evidence — the cert pool is candidate-only by contract)."""
    sys.path.insert(0, str(ROOT.parent / "tools"))
    import cert_deps
    return cert_deps.dep_flags(MANIFEST, ARTIFACT, LOCK)


def src_files():
    """Every .drift under microflows's manifest src dirs. ABSOLUTE paths: a
    job's `cmd` must resolve regardless of the invoking process's cwd (a
    combined root-level plan runs from one cwd across several components'
    sources), matching integration/coordinator-singular's emitter."""
    m = json.loads(MANIFEST.read_text())
    art = next((a for a in m.get("artifacts", []) if a["name"] == ARTIFACT), None)
    if not art:
        sys.exit(f"error: artifact {ARTIFACT!r} not in manifest")
    dirs = sorted({os.path.dirname(mod) for mod in art.get("modules", [])})
    seen = set()
    for d in dirs:
        p = ROOT / d
        if p.is_dir():
            for f in p.rglob("*.drift"):
                seen.add(str(f))
    return sorted(seen)


def is_test_entry(rel):
    # drift >= 0.33.67 requires the --entry target to be `pub`, so `main` may be
    # declared `pub fn main` (older tests still use bare `fn main`).
    txt = (ROOT / rel).read_text(errors="ignore")
    return bool(re.search(r"^module\s+", txt, re.M)) and bool(re.search(r"^(?:pub\s+)?fn\s+main\(", txt, re.M))


def module_of(rel):
    m = re.search(r"^module\s+(.+?);?\s*$", (ROOT / rel).read_text(errors="ignore"), re.M)
    if not m:
        sys.exit(f"error: missing module declaration in {rel}")
    return m.group(1).strip().rstrip(";")


def _sanitize(on):
    return ["--sanitize", "address" if on else "none"]


def src_build(out_name, srcs, dep_flags, entry, test_rel, sanitize=False):
    """Build job: compile test_rel + microflows's src tree to {work}/<out_name>."""
    out = f"{{work}}/{out_name}"
    cmd = (["{driftc}", "--target-word-bits", TWB, "--package-root", PKG_ROOT]
           + dep_flags + _sanitize(sanitize) + ["--entry", entry] + srcs + [test_rel, "-o", out])
    return {"id": out_name, "out": out, "cmd": cmd}


def _entries(root):
    # ABSOLUTE (see src_files() above) — is_test_entry/module_of still work: an
    # absolute `rel` makes `ROOT / rel` return `rel` unchanged (pathlib: an
    # absolute right operand replaces the whole path).
    p = ROOT / root
    return [str(f) for f in sorted(p.glob("*.drift"))
            if p.is_dir() and is_test_entry(str(f))]


# ------------------------------------------------------------------ gate: test
def emit_test():
    srcs, dep_flags = src_files(), resolved_dep_flags()
    build, run_unit, run_live = [], [], []

    # Unit — DB-free; run base / memcheck (reuse base via wrap) / asan in PARALLEL.
    for rel in _entries(UNIT_ROOT):
        qual = f"{ARTIFACT}-unit-{Path(rel).stem}"
        entry = f"{module_of(rel)}::main"
        build.append(src_build(f"{qual}#base", srcs, dep_flags, entry, rel))
        build.append(src_build(f"{qual}#asan", srcs, dep_flags, entry, rel, sanitize=True))
        run_unit.append({"id": f"{qual}#run-base", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"]})
        run_unit.append({"id": f"{qual}#run-memcheck", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"], "wrap": "memcheck"})
        run_unit.append({"id": f"{qual}#run-asan", "cmd": [f"{{work}}/{qual}#asan"], "needs": [f"{qual}#asan"]})

    # e2e — build base+asan; run base/asan/memcheck SERIAL on the instance DB
    # group, each lane on its own fixture namespace (per-lane + per-invocation).
    live_quals = []
    for rel in LIVE_TESTS:
        if not is_test_entry(rel):
            sys.exit(f"error: {rel} is not an executable test entry (module + fn main)")
        qual = f"{ARTIFACT}-e2e-{Path(rel).stem}"
        entry = f"{module_of(rel)}::main"
        abs_rel = str(ROOT / rel)   # LIVE_TESTS entries are component-relative; job cmd needs absolute
        build.append(src_build(f"{qual}#base", srcs, dep_flags, entry, abs_rel))
        build.append(src_build(f"{qual}#asan", srcs, dep_flags, entry, abs_rel, sanitize=True))
        live_quals.append(qual)

    order = 0
    for lane in ("base", "asan", "memcheck"):
        for qual in live_quals:
            if lane == "asan":
                job = {"id": f"{qual}#run-asan", "cmd": [f"{{work}}/{qual}#asan"], "needs": [f"{qual}#asan"]}
            elif lane == "memcheck":
                job = {"id": f"{qual}#run-memcheck", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"], "wrap": "memcheck"}
            else:
                job = {"id": f"{qual}#run-base", "cmd": [f"{{work}}/{qual}#base"], "needs": [f"{qual}#base"]}
            job.update({"mode": "serial", "group": DB_GROUP, "order": order,
                        "env": {"MICROFLOWS_E2E_NS": f"drift-test-{NONCE}-{lane}"}})
            order += 1
            run_live.append(job)

    phases = [
        {"name": "build", "jobs": build},
        {"name": "run-unit", "jobs": run_unit},
    ]
    if run_live:
        phases.append({"name": "run-live", "jobs": run_live})
    return {"name": "test", "phases": phases}


# ----------------------------------------------------------- dev: one / compile
def emit_one(rel):
    if not is_test_entry(rel):
        sys.exit(f"error: {rel} is not an executable test entry (module + fn main)")
    srcs, dep_flags = src_files(), resolved_dep_flags()
    name = Path(rel).stem
    build = [src_build(name, srcs, dep_flags, f"{module_of(rel)}::main", str(ROOT / rel))]
    run_job = {"id": f"{name}#run", "cmd": [f"{{work}}/{name}"], "needs": [name]}
    if "/tests/e2e/" in rel:
        run_job["env"] = {"MICROFLOWS_E2E_NS": f"drift-test-{NONCE}-one"}
    return {"name": "one", "phases": [
        {"name": "build", "jobs": build},
        {"name": "run", "jobs": [run_job]},
    ]}


def emit_compile(rel):
    srcs, dep_flags = src_files(), resolved_dep_flags()
    abs_rel = str(ROOT / rel)
    extra = [] if abs_rel in srcs else [abs_rel]  # don't pass a src file twice (srcs are absolute)
    cmd = (["{driftc}", "--target-word-bits", TWB, "--package-root", PKG_ROOT]
           + dep_flags + _sanitize(False) + srcs + extra)
    return {"name": "compile", "phases": [{"name": "compile", "jobs": [{"id": "compile-check", "cmd": cmd}]}]}


def main():
    global DB_GROUP
    ap = argparse.ArgumentParser(description="Emit a drift_test_run.py plan for a Microflows gate.")
    ap.add_argument("gate", choices=["test", "one", "compile"])
    ap.add_argument("--file", help="test/source file (for one|compile)")
    ap.add_argument("--out", default="-", help="output path for the plan JSON (default: stdout)")
    ap.add_argument("--db-group", default=DB_GROUP,
                    help="serial flocker group for DB-backed jobs (default: the shared host-global "
                         "key; a locked phase passes a non-shared key to avoid self-deadlock)")
    args = ap.parse_args()
    DB_GROUP = args.db_group
    if args.gate in ("one", "compile"):
        if not args.file:
            sys.exit("error: --file required for one|compile")
        plan = emit_one(args.file) if args.gate == "one" else emit_compile(args.file)
    else:
        plan = emit_test()
    text = json.dumps(plan, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
        n = sum(len(p["jobs"]) for p in plan["phases"])
        print(f"wrote {args.out}: {plan['name']} plan, {n} jobs across {len(plan['phases'])} phase(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
