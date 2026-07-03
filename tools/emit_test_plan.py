#!/usr/bin/env python3
"""Emit ONE combined drift_test_run.py plan for the root `just test` gate.

Root-level PLAN COMBINER — not a new emitter of policy. It imports the three
existing, independently-maintained per-component emitters (singular's,
microflows's, integration/coordinator-singular's) and merges their `build` /
`run-unit` / `run-live` job lists into one plan, handed to ONE
`drift_test_run.py` invocation. Each component's own emitter (and its own
`just test-<component>` standalone recipe) is unchanged and still works on
its own — this script only changes how the ROOT gate schedules the same work.

Why: previously the root gate ran `test-singular`, `test-microflows`,
`test-integration` as three fully sequential `just` invocations. Each already
parallelizes its OWN compiles under the shared executor's flocker pool, but
there was zero overlap ACROSS the three, and `microflows/runner/justfile`'s
`build` recipe and `integration/coordinator-singular/tools/emit_test_plan.py`'s
`microflows-runner` app entry compiled the IDENTICAL source closor (same
trust store, same entry `microflows.runner::main`) into two separate
binaries. Combining into one plan lets every DB-free compile across all
four sources run in one pool, and the `out`-dedup mechanism means the
runner binary is now built exactly once.

Path handling: singular's and microflows's emitters were fixed (separately)
to hand back ABSOLUTE paths in every job `cmd`, so their jobs are safe to run
from any cwd. integration's emitter was already absolute-path-safe. This
script itself needs no `--db-group`-passing dance: it runs from the repo
root, in one process, so job ids/groups are combined directly.

Job execution has no `cwd` field (drift_test_run.py's Job dataclass doesn't
support one) — DB-touching jobs that used to run from a component's own
directory (mariachi schema resets, SP regressions, the coordinator harness)
are wrapped in `bash -c "cd <abs dir> && ..."` here so their own internal
relative-path assumptions keep working unchanged.

Usage: tools/emit_test_plan.py test [--out FILE]
"""
import argparse
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root (tools/ -> repo root)
TWB = "64"

SINGULAR_ROOT = ROOT / "singular" / "drift"      # the Drift binding (source, drift/manifest.json)
SINGULAR_DB_ROOT = ROOT / "singular"              # the shared backend (db/, db-tests/) -- a SIBLING
                                                   # of drift/, not nested under it
MICROFLOWS_ROOT = ROOT / "microflows"
INTEGRATION_ROOT = ROOT / "integration" / "coordinator-singular"
RUNNER_ROOT = MICROFLOWS_ROOT / "runner"

# The shared host-global DB *instance* mutex key, matching every per-component
# emitter's own DB_GROUP default/`--db-group` convention. The root recipe
# acquires $DB_LOCK once around the whole combined-plan invocation; jobs here
# use $DB_HELD_GROUP (a distinct, non-shared key) for their own serial group,
# so the executor's per-job flocker lock can't deadlock against that outer
# hold (flocker is not re-entrant) -- same protocol as every existing recipe.
DB_GROUP = os.environ.get("DB_HELD_GROUP", "mariadb-mdb114-a")


def _load_module(name, path):
    """Load a same-named sibling emitter (all three are literally
    `emit_test_plan.py`) under a distinct module name so they don't collide."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SINGULAR = _load_module("_root_emit_singular", SINGULAR_ROOT / "tools" / "emit_test_plan.py")
MICROFLOWS = _load_module("_root_emit_microflows", MICROFLOWS_ROOT / "tools" / "emit_test_plan.py")
INTEGRATION = _load_module("_root_emit_integration", INTEGRATION_ROOT / "tools" / "emit_test_plan.py")


def _jobs_of(plan, phase_name):
    for ph in plan["phases"]:
        if ph["name"] == phase_name:
            return ph["jobs"]
    return []


def _in_dir(dir_abs, argv, env=None):
    """Wrap argv to run with cwd=dir_abs (Job has no cwd field). `env` values
    are baked into the bash script itself, NOT passed via Job.env: the
    executor's `{work}`/`{driftc}`/... substitution only runs over `cmd`
    tokens (`build_argv`'s `substitute(job.cmd, subs)`) -- `job.env` is used
    verbatim (`run_job`'s `env.update(job.env)`), so a `{work}`-relative path
    passed through Job.env would reach the child process unsubstituted."""
    prefix = ""
    if env:
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "
    script = "cd " + shlex.quote(str(dir_abs)) + " && " + prefix + " ".join(shlex.quote(a) for a in argv)
    return ["bash", "-c", script]


def _reordered(jobs, group, start):
    """Copy `jobs` (already-correctly-ordered run-live jobs from a component's
    own emitter), reassigning `group`/`order` to fold them into ONE combined
    serial chain while preserving their existing relative order."""
    out = []
    for i, j in enumerate(jobs):
        j = dict(j)
        j["group"] = group
        j["order"] = start + i
        out.append(j)
    return out


# ------------------------------------------------------------- runner: ir tests
# ir_graph_test / ir_exec_test compile standalone (ir.drift imports only
# std.*) -- no --package-root, no --dep, matching runner/justfile's own
# hand-rolled loop this replaces. That loop ran the two tests x two variants
# fully sequentially; folding them into the shared pool lets them overlap
# with everything else.
def _runner_ir_jobs():
    build, run_unit = [], []
    src = str(RUNNER_ROOT / "src" / "ir.drift")
    for t in ("ir_graph_test", "ir_exec_test"):
        test_src = str(RUNNER_ROOT / "tests" / "unit" / f"{t}.drift")
        entry = f"microflows.runner.tests.{t}::main"
        for variant, sanitize in (("base", False), ("asan", True)):
            out_name = f"runner-{t}#{variant}"
            out = f"{{work}}/{out_name}"
            cmd = ["{driftc}", "--target-word-bits", TWB,
                   "--sanitize", "address" if sanitize else "none",
                   "--entry", entry, src, test_src, "-o", out]
            build.append({"id": out_name, "out": out, "cmd": cmd})
            run_unit.append({"id": f"{out_name}#run", "cmd": [out], "needs": [out_name]})
    return build, run_unit


# runner/tests/run_parser_fixtures.py + run_manifest_fixtures.py: DB-free,
# data-driven checks against the already-built runner binary (`--bin`).
def _runner_fixture_jobs(runner_bin_out):
    jobs = []
    for script, jid in (("run_parser_fixtures.py", "runner-parser-fixtures"),
                        ("run_manifest_fixtures.py", "runner-manifest-fixtures")):
        jobs.append({
            "id": jid,
            "cmd": [sys.executable, str(RUNNER_ROOT / "tests" / script), "--bin", runner_bin_out],
            "needs": ["microflows-runner"],
        })
    return jobs


# ------------------------------------------------------- DB-touching: resets
def _mariachi_argv(schema_template_abs, schema, extra):
    mariachi_bin = os.environ.get("MARIACHI_BIN")
    if not mariachi_bin:
        sys.exit("error: MARIACHI_BIN not set (source tools/cert-env.sh first)")
    return [mariachi_bin,
            "--schema-template", str(schema_template_abs),
            "--host", os.environ.get("DB_HOST", "127.0.0.1"),
            "--port", os.environ.get("DB_PORT", "34214"),
            "--user", os.environ.get("DB_USER", "root"),
            "--password-env", "MDB_ROOT_PWD"] + extra + ["--schema", schema]


def _reset_singular_job():
    """Mirrors singular/justfile's `_db-load-schema`: product schema + the
    malformed-backend fixture schema, both destructive resets."""
    a = _mariachi_argv(SINGULAR_DB_ROOT / "db", "singular",
                       ["apply", "--env=development", "--allow-destructive", "--destroy-database"])
    b = _mariachi_argv(SINGULAR_DB_ROOT / "db-tests" / "malformed", "singular_malformed",
                       ["apply", "--env=development", "--allow-destructive", "--destroy-database"])
    script = " && ".join(" ".join(shlex.quote(x) for x in cmd) for cmd in (a, b))
    return {"id": "reset-singular-schema", "cmd": ["bash", "-c", script]}


def _reset_microflows_job():
    """Mirrors microflows/justfile's `_db-load-schema` + `_db-load-test-fixtures`:
    product schema + the test-only fixture-proc schema, both destructive resets."""
    a = _mariachi_argv(MICROFLOWS_ROOT / "db", "microflows",
                       ["apply", "--env=development", "--allow-destructive", "--destroy-database"])
    b = _mariachi_argv(MICROFLOWS_ROOT / "db-tests" / "seed", "microflows_test",
                       ["apply", "--env", "development", "--allow-destructive", "--destroy-database"])
    script = " && ".join(" ".join(shlex.quote(x) for x in cmd) for cmd in (a, b))
    return {"id": "reset-microflows-schema", "cmd": ["bash", "-c", script]}


def _reset_coordinator_fixtures_job():
    """Mirrors integration/coordinator-singular/justfile's `_db-and-harness`
    reset step: OVERWRITES both schemas with the coordinator-fixtures scenario
    -- must run AFTER every other run-live job that needs the product schemas
    in their OWN (non-coordinator) shape."""
    a = _mariachi_argv(SINGULAR_DB_ROOT / "db", "singular",
                       ["apply", "--env=development", "--allow-destructive", "--destroy-database"])
    b = _mariachi_argv(MICROFLOWS_ROOT / "db-tests" / "coordinator", "microflows",
                       ["scenario", "--name", "coordinator-fixtures"])
    script = " && ".join(" ".join(shlex.quote(x) for x in cmd) for cmd in (a, b))
    return {"id": "reset-coordinator-fixtures", "cmd": ["bash", "-c", script]}


# --------------------------------------------------------- DB-touching: SP regressions
def _mariachi_python():
    mariachi_bin = os.environ.get("MARIACHI_BIN")
    if not mariachi_bin:
        sys.exit("error: MARIACHI_BIN not set (source tools/cert-env.sh first)")
    return str(Path(mariachi_bin).parent / "python")


def _microflows_sp_regression_jobs(runner_bin_out):
    py = _mariachi_python()
    jobs = [
        {"id": "microflows-sp-operation-regression",
         "cmd": _in_dir(MICROFLOWS_ROOT, [py, "db-tests/sp_operation_test.py"])},
        {"id": "microflows-sp-call-regression",
         "cmd": _in_dir(MICROFLOWS_ROOT, [py, "db-tests/sp_call_test.py"])},
        {"id": "microflows-call-integration-regression",
         "cmd": _in_dir(MICROFLOWS_ROOT, [py, "db-tests/call_integration_test.py"],
                        env={"MF_RUNNER_BIN": runner_bin_out}),
         "needs": ["microflows-runner"]},
    ]
    return jobs


# --------------------------------------------------------- DB-touching: coordinator harness
def _coordinator_harness_job(participant_stub_out, runner_bin_out, service_bin_out):
    return {
        "id": "coordinator-singular-harness",
        "cmd": _in_dir(INTEGRATION_ROOT, [sys.executable, "test.py"],
                       env={"STUB_BIN": participant_stub_out, "RUNNER_BIN": runner_bin_out,
                            "SERVICE_BIN": service_bin_out}),
        "needs": ["participant-stub", "microflows-runner", "uflowsd"],
    }


# ------------------------------------------------------------------ gate: test
def emit_test():
    singular_plan = SINGULAR.emit_test()
    microflows_plan = MICROFLOWS.emit_test()
    integration_plan = INTEGRATION.emit_test()

    build = []
    build += _jobs_of(singular_plan, "build")
    build += _jobs_of(microflows_plan, "build")
    # Already includes participant-stub, microflows-runner (== mfrunner --
    # same sources/entry/trust-store, confirmed via microflows/runner/drift
    # /manifest.json's first artifact), and uflowsd -- one compile each.
    build += _jobs_of(integration_plan, "build")

    ir_build, ir_run_unit = _runner_ir_jobs()
    build += ir_build

    run_unit = []
    run_unit += _jobs_of(singular_plan, "run-unit")
    run_unit += _jobs_of(microflows_plan, "run-unit")
    run_unit += ir_run_unit
    run_unit += _runner_fixture_jobs("{work}/microflows-runner")

    # run-live: one shared serial chain -- schema resets as jobs, then each
    # component's own DB-backed tests in their existing relative order, then
    # microflows's SP regressions, then integration's OWN reset (which
    # OVERWRITES both schemas) and its harness, strictly last.
    run_live = []
    order = 0
    run_live.append(dict(_reset_singular_job(), mode="serial", group=DB_GROUP, order=order)); order += 1
    run_live.append(dict(_reset_microflows_job(), mode="serial", group=DB_GROUP, order=order)); order += 1

    singular_live = _jobs_of(singular_plan, "run-live")
    run_live += _reordered(singular_live, DB_GROUP, order); order += len(singular_live)

    microflows_live = _jobs_of(microflows_plan, "run-live")
    run_live += _reordered(microflows_live, DB_GROUP, order); order += len(microflows_live)

    for j in _microflows_sp_regression_jobs("{work}/microflows-runner"):
        run_live.append(dict(j, mode="serial", group=DB_GROUP, order=order)); order += 1

    run_live.append(dict(_reset_coordinator_fixtures_job(), mode="serial", group=DB_GROUP, order=order)); order += 1
    run_live.append(dict(_coordinator_harness_job("{work}/participant-stub", "{work}/microflows-runner",
                                                    "{work}/uflowsd"),
                          mode="serial", group=DB_GROUP, order=order)); order += 1

    return {"name": "root-combined-test", "phases": [
        {"name": "build", "jobs": build},
        {"name": "run-unit", "jobs": run_unit},
        {"name": "run-live", "jobs": run_live},
    ]}


def main():
    ap = argparse.ArgumentParser(description="Emit ONE combined drift_test_run.py plan for the root `just test` gate.")
    ap.add_argument("gate", choices=["test"])
    ap.add_argument("--out", default="-", help="output path for the plan JSON (default: stdout)")
    args = ap.parse_args()
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
