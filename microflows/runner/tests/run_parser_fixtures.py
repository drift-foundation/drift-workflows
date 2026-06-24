#!/usr/bin/env python3
"""Data-driven parser/IR test for the Microflows textual frontend.

Replaces the old compiled-in `tests/unit/parser_test.drift` (which inlined ~60 `.mf` scenarios as
Drift string literals into ONE driftc translation unit, costing multiple GB / minutes to BUILD). The
scenarios are now data files under `tests/fixtures/parser/`; the already-built `microflows-runner`
binary reads them at runtime (kilobytes of RAM, milliseconds each):

  check/<name>.mf  -> `microflows-runner --parse-check <f>`  : canonical JSON outcome on stdout
  lower/<name>.mf  -> `microflows-runner --lower-source <f> --config <base>` : merged config / diagnostic

Each fixture has a committed golden `<name>.expected` (JSON). A run compares; `--update` regenerates
(bless from the currently-passing parser). Usage:
  run_parser_fixtures.py --bin <microflows-runner> [--root <fixtures>] [--update]
"""
import argparse, json, os, subprocess, sys

def canon(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)

def parse_check_outcome(bin_path, mf):
    p = subprocess.run([bin_path, "--parse-check", mf], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"--parse-check exited {p.returncode} (expected 0; outcome is in JSON)\nstderr: {p.stderr}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"--parse-check stdout is not JSON: {e}\nstdout: {p.stdout!r}\nstderr: {p.stderr}")

def _diag_from_stderr(stderr):
    """Extract the structured `lower-source-parse-error` event (stable code + position fields)."""
    for line in stderr.splitlines():
        line = line.strip()
        if not (line.startswith("{") and "lower-source-parse-error" in line):
            continue
        attrs = json.loads(line).get("attrs", {})
        out = {"outcome": "error", "kind": "parse"}
        for k in ("code", "expected", "found"):
            out[k] = attrs.get(k, "")
        for k in ("byte_offset", "line", "column"):
            out[k] = int(attrs.get(k, "0"))
        return out
    return None

def lower_outcome(bin_path, mf, base):
    p = subprocess.run([bin_path, "--lower-source", mf, "--config", base], capture_output=True, text=True)
    if p.returncode == 0:
        return {"outcome": "ok", "config": json.loads(p.stdout)}
    diag = _diag_from_stderr(p.stderr)
    if diag is not None:
        return diag
    # a build-time validation rejection (no structured parse diagnostic) — golden the message line.
    msg = ""
    for line in p.stderr.splitlines():
        if "invalid lowered config" in line:
            msg = line.split("invalid lowered config:", 1)[1].strip()
    return {"outcome": "error", "kind": "validate", "message": msg}

def base_for(lower_dir, name):
    specific = os.path.join(lower_dir, name + ".base.json")
    return specific if os.path.exists(specific) else os.path.join(lower_dir, "_base.json")

def run(root, bin_path, update):
    fails, total, updated = [], 0, 0
    # check fixtures
    cdir = os.path.join(root, "check")
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".mf"):
            continue
        total += 1
        name = fn[:-3]
        mf = os.path.join(cdir, fn)
        gold = os.path.join(cdir, name + ".expected")
        try:
            got = parse_check_outcome(bin_path, mf)
        except RuntimeError as e:
            fails.append((f"check/{name}", str(e)))
            continue
        if update:
            open(gold, "w").write(canon(got) + "\n"); updated += 1; continue
        if not os.path.exists(gold):
            fails.append((f"check/{name}", "no golden (run --update to bless)")); continue
        want = json.load(open(gold))
        if got != want:
            fails.append((f"check/{name}", f"mismatch\n  want: {canon(want)}\n  got:  {canon(got)}"))
    # lower fixtures
    ldir = os.path.join(root, "lower")
    for fn in sorted(os.listdir(ldir)):
        if not fn.endswith(".mf"):
            continue
        total += 1
        name = fn[:-3]
        mf = os.path.join(ldir, fn)
        gold = os.path.join(ldir, name + ".expected")
        got = lower_outcome(bin_path, mf, base_for(ldir, name))
        if update:
            open(gold, "w").write(canon(got) + "\n"); updated += 1; continue
        if not os.path.exists(gold):
            fails.append((f"lower/{name}", "no golden (run --update to bless)")); continue
        want = json.load(open(gold))
        if got != want:
            fails.append((f"lower/{name}", f"mismatch\n  want: {canon(want)}\n  got:  {canon(got)}"))
    if update:
        print(f"[parser-fixtures] updated {updated} golden(s)")
        return 0
    for name, why in fails:
        print(f"FAIL {name}: {why}", file=sys.stderr)
    print(f"[parser-fixtures] {total - len(fails)}/{total} passed")
    return 1 if fails else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=os.environ.get("MF_RUNNER_BIN", ""), help="microflows-runner binary")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "parser"))
    ap.add_argument("--update", action="store_true", help="(re)generate goldens from the binary")
    a = ap.parse_args()
    if not a.bin or not os.path.exists(a.bin):
        print(f"error: --bin not found: {a.bin!r} (build it: just build)", file=sys.stderr); return 2
    return run(a.root, a.bin, a.update)

if __name__ == "__main__":
    sys.exit(main())
