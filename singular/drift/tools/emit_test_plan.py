#!/usr/bin/env python3
"""Emit a drift_test_run.py plan for a Singular gate or a single dev test.

Singular's PLAN EMITTER — the small per-project POLICY piece kept once the shared
toolchain executor (`$DRIFT_TOOLCHAIN_ROOT/lib/tools/drift_test_run.py`) owns the
mechanism (parallel compile under the flocker pool, run scheduling, dedup,
valgrind wrap, heartbeat, host concurrency budget). See doc/test-run.md +
doc/certifiable-test-gates.md in the toolchain bundle.

Gates:
  test  — unit + e2e, each built {base, asan}. Unit runs base/memcheck/asan in
          PARALLEL (DB-free). e2e runs base/asan/memcheck SERIALIZED on the shared
          MariaDB instance via one `mode:serial group:<DB_GROUP>` — one DB access
          at a time across this gate AND any concurrent cert lane (the key is the
          *instance*, not the database; resource contention, not state).
Dev:
  one --file F     — build + run one test (base), for fast iteration.
  compile --file F — type-check one file against singular's sources (no run).

Singular-specific vs. the mariadb-client reference:
  - SINGLE artifact ("singular"); its deps (mariadb-rpc, mariadb-wire-proto) are
    EXTERNAL (not co-artifacts), so src-builds carry `--package-root $DRIFT_PKG_ROOT`
    and `--dep name@<lock version>` for the FULL resolved (incl. transitive) set
    read from drift/lock.json — never hardcoded.
  - FIXTURE ISOLATION (the reason this emitter mints env): Singular's terminal
    records are immutable (a completed idempotency key stays completed), so the
    same e2e binary re-run on the same (service_group, key) would see AlreadyDone
    and fail. Each e2e run-job gets a per-job `env` SINGULAR_E2E_SVCGROUP =
    drift-test-<nonce>-<lane>: <nonce> is per-INVOCATION (re-runs need virgin
    space too), <lane> per-lane so base/asan/memcheck don't collide within one
    run. The test reads it as its service_group (see _e2e_service_group in
    live_gateway_test.drift). This is the sanctioned per-lane-isolation idiom.

Naming: every build `out` is `singular-<leaf>-<stem>#<variant>`, DOT-FREE (dashes)
so pre-0.33.16 scratch-IR paths can't collide; `out` lands directly under {work}.
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
ARTIFACT = "singular"

# Foundation-libs root for the external mariadb deps. driftc needs an absolute
# --package-root; the recipe exports DRIFT_PKG_ROOT (cert env sets it too).
PKG_ROOT = os.path.abspath(os.environ.get(
    "DRIFT_PKG_ROOT", os.path.expanduser("~/opt/drift/certified/current/libs")))

# Host-global mutex key naming the shared MariaDB *instance* (mdb114-a @ :34114).
# Must match other consumers' string (mariadb-client) to serialize across suites
# on the one physical box — resource contention, not state (A1 isolates state).
# This DEFAULT serializes a direct executor run on the shared resource. An
# orchestrator that already holds the shared lock across a wider setup+DB-test
# phase passes a non-shared key via the `--db-group` CLI flag (a CONTROLLED arg
# from the locked recipe — never an ambient env var), so the executor's per-job
# lock can't deadlock against that outer hold (flocker is not re-entrant).
DB_GROUP = "mariadb-mdb114-a"

# DB-free unit tests (globbed for executable `fn main` entries).
UNIT_ROOT = "packages/singular/tests/unit"
# DB-backed e2e tests — curated + ordered (order is significant: serial on the DB).
# The malformed-backend fixture test is part of the gate (finding #1: the CORE_BUG
# regression must be pinned in cert, not a side recipe). It runs against the isolated
# `singular_malformed` schema, which `just db-load-schema` loads alongside the product
# schema; it is stateless (fixture SPs return literals) so it ignores SINGULAR_E2E_SVCGROUP.
LIVE_TESTS = [
    "packages/singular/tests/e2e/live_gateway_test.drift",
    "packages/singular/tests/fixtures/malformed_backend_test.drift",
]

# Per-invocation nonce → fresh fixture namespace each `just test` (required:
# append-only records, no delete API, so re-runs need virgin space too).
NONCE = f"{os.getpid()}-{time.time_ns()}"


def resolved_dep_flags():
    """Full resolved dep set (incl. transitive) for `singular`, from the lock —
    never hardcoded, so the emitter tracks lock bumps automatically."""
    lock = json.loads(LOCK.read_text())
    resolved = (lock.get("artifacts", {}).get(ARTIFACT, {}) or {}).get("resolved", {}) or {}
    if not resolved:
        sys.exit(f"error: no resolved deps for {ARTIFACT!r} in {LOCK} (run `just prepare`)")
    flags = []
    for name in sorted(resolved):
        flags += ["--dep", f"{name}@{resolved[name]['version']}"]
    return flags


def src_files():
    """Every .drift under singular's manifest src dirs (walked like a source
    build — picks up internal modules a test imports, not just manifest modules)."""
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
                seen.add(str(f.relative_to(ROOT)))
    return sorted(seen)


def is_test_entry(rel):
    txt = (ROOT / rel).read_text(errors="ignore")
    return bool(re.search(r"^module\s+", txt, re.M)) and bool(re.search(r"^fn\s+main\(", txt, re.M))


def module_of(rel):
    m = re.search(r"^module\s+(.+?);?\s*$", (ROOT / rel).read_text(errors="ignore"), re.M)
    if not m:
        sys.exit(f"error: missing module declaration in {rel}")
    return m.group(1).strip().rstrip(";")


def _sanitize(on):
    # Explicit argv selector (driftc --sanitize selects the matching runtime
    # archive too), never DRIFT_ASAN.
    return ["--sanitize", "address" if on else "none"]


def src_build(out_name, srcs, dep_flags, entry, test_rel, sanitize=False):
    """Build job: compile test_rel + singular's src tree to {work}/<out_name>,
    resolving the external mariadb deps via --package-root + --dep."""
    out = f"{{work}}/{out_name}"
    cmd = (["{driftc}", "--target-word-bits", TWB, "--package-root", PKG_ROOT]
           + dep_flags + _sanitize(sanitize) + ["--entry", entry] + srcs + [test_rel, "-o", out])
    return {"id": out_name, "out": out, "cmd": cmd}


def _entries(root):
    p = ROOT / root
    return [str(f.relative_to(ROOT)) for f in sorted(p.glob("*.drift"))
            if p.is_dir() and is_test_entry(str(f.relative_to(ROOT)))]


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
        build.append(src_build(f"{qual}#base", srcs, dep_flags, entry, rel))
        build.append(src_build(f"{qual}#asan", srcs, dep_flags, entry, rel, sanitize=True))
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
                        "env": {"SINGULAR_E2E_SVCGROUP": f"drift-test-{NONCE}-{lane}"}})
            order += 1
            run_live.append(job)

    # Raw-SQL / SP-invariant track: drives the SPs directly via pymysql (cases Drift can't express —
    # SQL NULL args, deliberate backend corruption, table-count assertions). Same runner + DB_GROUP
    # serialization + isolation policy as the other DB tests (per-run nonce service_group inside the
    # script). No build dependency. The mariachi venv provides pymysql.
    sp_python = str(ROOT.parent.parent.parent / "mariachi" / ".venv" / "bin" / "python")
    sp_script = str(ROOT / "packages" / "singular" / "tests" / "sql" / "sp_invariants_test.py")
    run_live.append({"id": f"{ARTIFACT}-sp-invariants", "cmd": [sp_python, sp_script],
                     "needs": [], "mode": "serial", "group": DB_GROUP, "order": order})

    return {"name": "test", "phases": [
        {"name": "build", "jobs": build},
        {"name": "run-unit", "jobs": run_unit},
        {"name": "run-live", "jobs": run_live},
    ]}


# ----------------------------------------------------------- dev: one / compile
def emit_one(rel):
    if not is_test_entry(rel):
        sys.exit(f"error: {rel} is not an executable test entry (module + fn main)")
    srcs, dep_flags = src_files(), resolved_dep_flags()
    name = Path(rel).stem
    build = [src_build(name, srcs, dep_flags, f"{module_of(rel)}::main", rel)]
    # An e2e single-run still needs a fixture namespace.
    run_job = {"id": f"{name}#run", "cmd": [f"{{work}}/{name}"], "needs": [name]}
    if "/tests/e2e/" in rel:
        run_job["env"] = {"SINGULAR_E2E_SVCGROUP": f"drift-test-{NONCE}-one"}
    return {"name": "one", "phases": [
        {"name": "build", "jobs": build},
        {"name": "run", "jobs": [run_job]},
    ]}


def emit_compile(rel):
    srcs, dep_flags = src_files(), resolved_dep_flags()
    extra = [] if rel in srcs else [rel]  # don't pass a src file twice
    cmd = (["{driftc}", "--target-word-bits", TWB, "--package-root", PKG_ROOT]
           + dep_flags + _sanitize(False) + srcs + extra)
    return {"name": "compile", "phases": [{"name": "compile", "jobs": [{"id": "compile-check", "cmd": cmd}]}]}


# --------------------------------------------------------------- gate: stress
# Concurrency/contention gate: build the lease-contention scenario, run it ONCE,
# serialized on the shared DB group (it spawns its own worker fan-out internally).
STRESS_SRC = "packages/singular/tests/stress/lease_contention_stress.drift"


def emit_stress():
    srcs, dep_flags = src_files(), resolved_dep_flags()
    entry = f"{module_of(STRESS_SRC)}::main"
    build = [src_build("singular-stress", srcs, dep_flags, entry, STRESS_SRC)]
    run = [{"id": "singular-stress#run", "cmd": [f"{{work}}/singular-stress"],
            "needs": ["singular-stress"], "mode": "serial", "group": DB_GROUP, "order": 0}]
    return {"name": "singular-stress",
            "phases": [{"name": "build", "jobs": build}, {"name": "run", "jobs": run}]}


# ----------------------------------------------------------------- gate: perf
# BUILD-ONLY plan: compile the perf scenario to {work}/singular-perf. The serial DB-backed RUN +
# baseline gate happens in the harness (tools/perf_gate.py), bracketing the executor (the
# drift-mariadb-client convention: parallel compile here, exclusive measurement outside).
PERF_SRC = "packages/singular/tests/perf/lease_cycle_perf.drift"


def emit_perf():
    srcs, dep_flags = src_files(), resolved_dep_flags()
    entry = f"{module_of(PERF_SRC)}::main"
    build = [src_build("singular-perf", srcs, dep_flags, entry, PERF_SRC)]
    return {"name": "singular-perf", "phases": [{"name": "build", "jobs": build}]}


def main():
    global DB_GROUP
    ap = argparse.ArgumentParser(description="Emit a drift_test_run.py plan for a Singular gate.")
    ap.add_argument("gate", choices=["test", "stress", "perf", "one", "compile"])
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
    elif args.gate == "stress":
        plan = emit_stress()
    elif args.gate == "perf":
        plan = emit_perf()
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
