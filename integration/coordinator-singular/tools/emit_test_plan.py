#!/usr/bin/env python3
"""Emit a drift_test_run.py BUILD plan for the coordinator<->singular integration.

Mirrors drift-web/tools/emit_test_plan.py: the project owns POLICY (which apps,
which library sources, which external deps); the shared executor owns mechanism
(parallel compile under flocker, work-dir, heartbeat).

The integration COMPILES both apps from CURRENT source — the runner against the
Microflows library sources, the participant stub against the Singular library
sources — resolving only EXTERNAL packages from DRIFT_PKG_ROOT (verified against
each app's committed trust store). It never deploys, signs, or mutates an author
claim: a clean checkout builds, and a stale package can't mask a regression.

Signed package deployment is a separate RELEASE concern, not a test-time step.

Usage: emit_test_plan.py test [--out FILE]
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]   # drift-workflows/ (tools/ -> suite -> integration -> repo)
TWB = "64"
PKG_ROOT = os.environ.get("DRIFT_PKG_ROOT",
                          os.environ.get("DRIFT_PACKAGE_ROOT",
                                         str(Path.home() / "opt/drift/certified/current/libs")))

# Each app is compiled from its OWN source + a local LIBRARY's source; every
# other resolved dependency is an external package from DRIFT_PKG_ROOT. Both the
# app and the library source closures are DERIVED FROM THEIR MANIFESTS (the
# `modules` list), so a new/nested module is picked up automatically — never
# hardcoded here.
#   app_proj  — project root holding the app's drift/{manifest,lock,trust}.json
#   lib_proj  — project root holding the local library's drift/manifest.json
#   local_lib — that library's artifact name (compiled from source, not consumed)
APPS = [
    {"out": "participant-stub", "app_proj": "microflows/participant-stub",
     "lib_proj": "singular/drift", "local_lib": "singular"},
    {"out": "microflows-runner", "app_proj": "microflows/runner",
     "lib_proj": "microflows", "local_lib": "microflows"},
    {"out": "uflowsd", "app_proj": "microflows/runner",
     "lib_proj": "microflows", "local_lib": "microflows", "artifact": "uflowsd"},
]


def _read_json(rel):
    return json.loads((ROOT / rel).read_text())


def _artifact(project_root, artifact_name=None):
    man = _read_json(f"{project_root}/drift/manifest.json")
    arts = man.get("artifacts", [])
    if not arts:
        sys.exit(f"error: no artifacts in {project_root}/drift/manifest.json")
    if artifact_name is None:
        return arts[0]
    art = next((a for a in arts if a["name"] == artifact_name), None)
    if not art:
        sys.exit(f"error: artifact {artifact_name!r} not in {project_root}/drift/manifest.json")
    return art


def _manifest_src_files(project_root, art):
    """The artifact's source closure: glob *.drift in the directory of every
    `modules` entry (mirrors drift-web — manifest-driven, so added modules are
    included). `modules` entries may be files or directories."""
    dirs = sorted({os.path.dirname(m) or "." for m in art.get("modules", [])})
    files = []
    for d in dirs:
        p = ROOT / project_root / d
        if not p.is_dir():
            sys.exit(f"error: module source dir not found: {project_root}/{d}")
        files += [str(f) for f in p.glob("*.drift")]
    files = sorted(set(files))
    if not files:
        sys.exit(f"error: no .drift sources for {art['name']} under {project_root}")
    return files


def _external_deps(app, artifact_name):
    """--dep flags for every RESOLVED dependency except the source-compiled local
    lib (read from the lock so transitive deps + version bumps track automatically)."""
    lock = _read_json(f"{app['app_proj']}/drift/lock.json")
    resolved = ((lock.get("artifacts", {}) or {}).get(artifact_name, {}) or {}).get("resolved", {}) or {}
    if not resolved:
        sys.exit(f"error: no resolved deps for {artifact_name} in {app['app_proj']}/drift/lock.json "
                 f"(run `just prepare` in that project)")
    flags = []
    for name in sorted(resolved):
        if name == app["local_lib"]:
            continue  # compiled from source, not consumed as a package
        flags += ["--dep", f"{name}@{resolved[name].get('version')}"]
    return flags


def _build_job(app):
    app_art = _artifact(app["app_proj"], app.get("artifact"))
    lib_art = _artifact(app["lib_proj"], app["local_lib"])
    trust = str(ROOT / f"{app['app_proj']}/drift/trust.json")
    srcs = (_manifest_src_files(app["lib_proj"], lib_art)
            + _manifest_src_files(app["app_proj"], app_art))
    out = f"{{work}}/{app['out']}"
    cmd = (["{driftc}", "--target-word-bits", TWB,
            "--trust-store", trust,
            "--package-root", PKG_ROOT]
           + _external_deps(app, app_art["name"])
           + ["--entry", app_art["entry_point"]]
           + srcs
           + ["-o", out])
    return {"id": app["out"], "out": out, "cmd": cmd}


def emit_test():
    return {"name": "coordinator-singular-build",
            "phases": [{"name": "build", "jobs": [_build_job(a) for a in APPS]}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gate", choices=["test"])
    ap.add_argument("--out", default="-")
    args = ap.parse_args()
    plan = emit_test()
    text = json.dumps(plan, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
        n = sum(len(p["jobs"]) for p in plan["phases"])
        print(f"wrote {args.out}: {plan['name']}, {n} build job(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
