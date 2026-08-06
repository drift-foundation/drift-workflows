#!/usr/bin/env python3
"""Focused coverage for tools/cert_deps.py enforce_toolchain_floor() — the repo's
driftc >= 0.35.0 gate (strict std.json acceptance contract).

Pins the review finding (2026-08-06, P2): a driftc that exits NONZERO while
printing plausible version JSON must be rejected — status is checked before
stdout is ever parsed. Each case fabricates a toolchain dir whose bin/driftc is
a shell stub with a controlled stdout/exit-status; no real toolchain is used.

Gate wiring: runs as the DB-free `cert-deps-floor-test` job in the root
combined plan (tools/emit_test_plan.py).
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cert_deps

AT_FLOOR_JSON = ('{"format":"drift-toolchain-info/v1","toolchain":'
                 '{"abi":22,"driftc":"%s","git":"deadbeef"}}')


def _fake_toolchain(tmp, stdout, exit_code):
    root = Path(tmp) / f"tc-{exit_code}-{abs(hash(stdout)) % 10**8}"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    driftc = bin_dir / "driftc"
    driftc.write_text("#!/bin/sh\nprintf '%s' " + _sh_quote(stdout) + f"\nexit {exit_code}\n")
    driftc.chmod(driftc.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return root


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


class EnforceToolchainFloorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _env_for(self, stdout, exit_code):
        root = _fake_toolchain(self._tmp.name, stdout, exit_code)
        return dict(os.environ, DRIFT_TOOLCHAIN_ROOT=str(root))

    def _floor_str(self):
        return ".".join(map(str, cert_deps.TOOLCHAIN_FLOOR))

    def test_nonzero_exit_rejected_even_with_valid_at_floor_json(self):
        # THE review repro: plausible at-floor JSON + exit 1 must fail closed.
        env = self._env_for(AT_FLOOR_JSON % self._floor_str(), 1)
        with self.assertRaises(SystemExit) as ctx:
            cert_deps.enforce_toolchain_floor(env)
        self.assertIn("exit 1", str(ctx.exception.code))

    def test_below_floor_rejected(self):
        env = self._env_for(AT_FLOOR_JSON % "0.34.1", 0)
        with self.assertRaises(SystemExit) as ctx:
            cert_deps.enforce_toolchain_floor(env)
        self.assertIn("below this repo's floor", str(ctx.exception.code))

    def test_at_floor_accepted(self):
        env = self._env_for(AT_FLOOR_JSON % self._floor_str(), 0)
        cert_deps.enforce_toolchain_floor(env)   # must not raise

    def test_above_floor_accepted(self):
        major, minor, patch = cert_deps.TOOLCHAIN_FLOOR
        env = self._env_for(AT_FLOOR_JSON % f"{major}.{minor}.{patch + 1}", 0)
        cert_deps.enforce_toolchain_floor(env)   # must not raise

    def test_garbage_stdout_rejected(self):
        env = self._env_for("driftc 0.35.0 (ABI 22)", 0)   # human format, not the JSON contract
        with self.assertRaises(SystemExit) as ctx:
            cert_deps.enforce_toolchain_floor(env)
        self.assertIn("cannot read driftc version", str(ctx.exception.code))

    def test_missing_driftc_rejected(self):
        env = dict(os.environ, DRIFT_TOOLCHAIN_ROOT=str(Path(self._tmp.name) / "nonexistent"))
        with self.assertRaises(SystemExit) as ctx:
            cert_deps.enforce_toolchain_floor(env)
        self.assertIn("driftc not found", str(ctx.exception.code))


if __name__ == "__main__":
    unittest.main()
