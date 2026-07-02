#!/usr/bin/env python3
"""Data-driven test for 1b.0's build-time registry validation gate (multi-script manifests).

Each fixture is a directory under tests/fixtures/manifest/<name>/ containing:
  manifest.json   -- the deployment manifest ("deployment" + "scripts": [{name, version, path}, ...])
  run.json        -- {"script": <name to submit>, "arguments": {...}}
  *.mf            -- the scripts the manifest references (relative paths)

`microflows-runner --manifest manifest.json --workflow-id <fixed> --script <run.script>
--arguments <run.arguments>` is run entirely DB-free for every fixture here: 1b.0's validation
(_load_manifest) always runs BEFORE any DB connection is attempted, so a rejection at ANY gate
(unresolved call target, bad input shape, bad result-path type, a static cycle, or the final
"static validation passed but runtime not implemented until 1b.1" gate) surfaces with zero DB/stub
setup. A fixture whose manifest is fully well-formed with NO reachable call would need a live DB to
actually dispatch — none of the fixtures here do (a reachable call always triggers the 1b.1-pending
rejection), so this suite stays DB-free by construction.

Golden format (<name>.expected, JSON): {"returncode": N, "stderr_contains": "<substring>"} — the
runner always exits nonzero for a manifest containing a reachable call (1b.0 keeps them
non-runnable), so every fixture here is a rejection case, distinguished by ITS OWN precise message.
"""
import argparse, json, os, subprocess, sys

WORKFLOW_ID = "0" * 32

def canon(obj):
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)

def run_fixture(bin_path, fdir):
    run_spec = json.load(open(os.path.join(fdir, "run.json")))
    cmd = [bin_path, "--manifest", os.path.join(fdir, "manifest.json"),
           "--workflow-id", WORKFLOW_ID, "--script", run_spec["script"],
           "--arguments", json.dumps(run_spec["arguments"])]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    return {"returncode": p.returncode, "stderr": p.stderr, "stdout": p.stdout}

def run(root, bin_path, update):
    fails, total, updated = [], 0, 0
    for name in sorted(os.listdir(root)):
        fdir = os.path.join(root, name)
        if not os.path.isdir(fdir):
            continue
        total += 1
        got = run_fixture(bin_path, fdir)
        gold = os.path.join(root, name + ".expected")
        if update:
            want = {"returncode": got["returncode"], "stderr_contains": got["stderr"].strip()}
            open(gold, "w").write(canon(want) + "\n")
            updated += 1
            continue
        if not os.path.exists(gold):
            fails.append((name, "no golden (run --update to bless)"))
            continue
        want = json.load(open(gold))
        ok = (got["returncode"] == want["returncode"]
              and want["stderr_contains"] in got["stderr"])
        if not ok:
            fails.append((name, f"want returncode={want['returncode']} stderr containing {want['stderr_contains']!r}\n"
                                 f"  got returncode={got['returncode']} stderr={got['stderr']!r}"))
    if update:
        print(f"[manifest-fixtures] updated {updated} golden(s)")
        return 0
    for name, why in fails:
        print(f"FAIL {name}: {why}", file=sys.stderr)
    print(f"[manifest-fixtures] {total - len(fails)}/{total} passed")
    return 1 if fails else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=os.environ.get("MF_RUNNER_BIN", ""), help="microflows-runner binary")
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "manifest"))
    ap.add_argument("--update", action="store_true", help="(re)generate goldens from the binary")
    a = ap.parse_args()
    if not a.bin or not os.path.exists(a.bin):
        print(f"error: --bin not found: {a.bin!r} (build it: just build)", file=sys.stderr); return 2
    return run(a.root, a.bin, a.update)

if __name__ == "__main__":
    sys.exit(main())
