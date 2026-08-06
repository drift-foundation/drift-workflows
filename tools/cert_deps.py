# tools/cert_deps.py — one derivation site for `--dep name@version` flags in gate compiles.
#
# Repo-root shim (the tools/cert-env.sh precedent): imported by the component/integration
# test-plan emitters and invoked as a CLI by the runner/participant-stub build recipes.
#
# Two lanes, per the build-orchestrator contract
# (/tmp/drift-announce/2026-07-31T012412Z-build-orchestrator-cert-gates-source-rebuild-answer.md)
# with the toolchain CLI that closed our source-checkout objection
# (2026-07-31T015625Z ask → 2026-07-31T042844Z: `drift lock emit --source-rebuild`
# ships in 0.33.92):
#
#   - STRICT (default, dev loop): the committed lock is the authoritative graph —
#     exact versions read from drift/lock.json. Stdlib-only; unchanged behavior.
#   - SOURCE-REBUILD (DRIFT_CERT_MODE=certify, exported by the orchestrator for
#     gate runs): the lock is EVIDENCE, not a gate. Resolution is ONE EXEC of the
#     run toolchain's own binary:
#         drift lock emit --artifact <name> --source-rebuild
#     which resolves via drift-lang's single source-rebuild authority (full
#     run-snapshot identity gating, real range semantics, structural trust
#     gates), honoring DRIFT_RUN_SNAPSHOT + DRIFT_PKG_ROOT from the standard
#     cert env. stdout is exactly the flags; evidence/diagnostics go to stderr;
#     errors fail closed (non-zero exit, empty stdout). Requires the run
#     toolchain >= 0.33.92 — an older toolchain rejects the flag, which is the
#     correct failure (never fall back to a hand-rolled resolver or a drift-lang
#     source import here; both were explicitly rejected in the notes above).

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Minimum driftc this repo compiles with. 0.35.0 is a hard floor, not advisory:
# std.json's permissive parse() profile only disappeared in the 0.33.93 clean
# break carried by this train — an older compiler accepts the same sources but
# silently parses every production JSON boundary permissively (duplicate keys,
# non-RFC numbers), defeating the strict-acceptance contract the packages
# version against. Coverage: dep-resolving compiles hit this via
# resolved_versions() (emitters import it; runner/participant-stub build
# recipes exec this module for their --dep flags); dep-FREE compiles (the
# standalone runner ir/unit tests) are covered because every plan emitter
# calls enforce_toolchain_floor() at emit time, and the runner justfile's
# local direct-driftc test loop runs `cert_deps.py --check-floor` first.
TOOLCHAIN_FLOOR = (0, 35, 0)


def enforce_toolchain_floor(env=None):
    """Fail-closed floor gate: exits unless $DRIFT_TOOLCHAIN_ROOT's driftc reports
    (via `--version --json`, exit 0) a version >= TOOLCHAIN_FLOOR."""
    env = os.environ if env is None else env
    toolchain = Path(env.get("DRIFT_TOOLCHAIN_ROOT") or os.path.expanduser("~/opt/drift/certified/current/toolchain"))
    driftc = toolchain / "bin" / "driftc"
    if not driftc.is_file():
        sys.exit(f"cert-deps: driftc not found at {driftc} (set DRIFT_TOOLCHAIN_ROOT)")
    proc = subprocess.run([str(driftc), "--version", "--json"], capture_output=True, text=True)
    floor = ".".join(map(str, TOOLCHAIN_FLOOR))
    # Nonzero exit is a failure REGARDLESS of stdout: a broken/hostile driftc can
    # emit plausible version JSON while dying — never parse past a bad status.
    if proc.returncode != 0:
        sys.exit(f"cert-deps: `{driftc} --version --json` failed (exit {proc.returncode}; "
                 f"stderr {proc.stderr.strip()!r}) — toolchain >= {floor} required")
    try:
        ver = json.loads(proc.stdout)["toolchain"]["driftc"]
        parsed = tuple(int(p) for p in ver.split("."))
    except Exception:
        sys.exit(f"cert-deps: cannot read driftc version from `{driftc} --version --json` "
                 f"(stdout {proc.stdout!r}) — toolchain >= {floor} required")
    if parsed < TOOLCHAIN_FLOOR:
        sys.exit(f"cert-deps: driftc {ver} is below this repo's floor {floor} "
                 f"(strict std.json acceptance contract; point DRIFT_TOOLCHAIN_ROOT at >= {floor})")


def _strict_versions(artifact, lock_path, exclude):
    with open(lock_path) as f:
        lock = json.load(f)
    resolved = {n: v["version"]
                for n, v in ((lock.get("artifacts", {}).get(artifact, {}) or {}).get("resolved", {}) or {}).items()
                if n not in exclude}
    if not resolved:
        sys.exit(f"error: no resolved deps for {artifact!r} in {lock_path} (run `just prepare`)")
    return resolved


def _certify_versions(manifest_path, artifact, exclude, env):
    toolchain = Path(env.get("DRIFT_TOOLCHAIN_ROOT") or os.path.expanduser("~/opt/drift/certified/current/toolchain"))
    drift = toolchain / "bin" / "drift"
    if not drift.is_file():
        sys.exit(f"cert-deps: drift CLI not found at {drift} (set DRIFT_TOOLCHAIN_ROOT)")
    proc = subprocess.run(
        [str(drift), "lock", "emit", "--artifact", artifact, "--manifest", str(manifest_path), "--source-rebuild"],
        capture_output=True, text=True, env=dict(env))
    # Evidence + diagnostics are the CLI's stderr contract — surface them verbatim.
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(f"cert-deps: `drift lock emit --artifact {artifact} --source-rebuild` failed "
                 f"(exit {proc.returncode}; toolchain >= 0.33.92 required)")
    tokens = proc.stdout.split()
    emitted = 0
    versions = {}
    for i, tok in enumerate(tokens):
        if tok != "--dep":
            continue
        if i + 1 >= len(tokens) or "@" not in tokens[i + 1]:
            sys.exit(f"cert-deps: `drift lock emit` stdout violates the flags contract "
                     f"(dangling --dep): {proc.stdout!r}")
        emitted += 1
        name, _, ver = tokens[i + 1].partition("@")
        if name not in exclude:
            versions[name] = ver
    if emitted == 0:
        sys.exit(f"cert-deps: `drift lock emit` produced no --dep flags for {artifact!r}")
    return versions  # may be empty if every emitted dep was excluded — caller's guard decides


def resolved_versions(manifest_path, artifact, lock_path, exclude=(), env=None):
    """{dep name: version} for `artifact` — the ONE derivation both emitters and
    build recipes must use. Strict lane: committed lock (authoritative). Certify
    lane (DRIFT_CERT_MODE=certify): the toolchain's `drift lock emit
    --source-rebuild`; lock demoted to evidence on stderr."""
    env = os.environ if env is None else env
    exclude = set(exclude)
    enforce_toolchain_floor(env)
    if env.get("DRIFT_CERT_MODE") == "certify":
        return _certify_versions(manifest_path, artifact, exclude, env)
    return _strict_versions(artifact, lock_path, exclude)


def dep_flags(manifest_path, artifact, lock_path, exclude=(), env=None):
    """['--dep', 'name@version', ...] sorted by name — drop-in for the emitters."""
    versions = resolved_versions(manifest_path, artifact, lock_path, exclude, env)
    flags = []
    for name in sorted(versions):
        flags += ["--dep", f"{name}@{versions[name]}"]
    return flags


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Emit --dep flags for a gate compile (strict lock / certify source-rebuild).")
    ap.add_argument("--check-floor", action="store_true",
                    help="only enforce the driftc floor (for dep-free direct-driftc recipes), no --dep output")
    ap.add_argument("--manifest")
    ap.add_argument("--artifact")
    ap.add_argument("--lock")
    ap.add_argument("--exclude", action="append", default=[])
    args = ap.parse_args()
    if args.check_floor:
        enforce_toolchain_floor()
        return
    if not (args.manifest and args.artifact and args.lock):
        ap.error("--manifest, --artifact and --lock are required unless --check-floor")
    print(" ".join(dep_flags(args.manifest, args.artifact, args.lock, args.exclude)))


if __name__ == "__main__":
    main()
