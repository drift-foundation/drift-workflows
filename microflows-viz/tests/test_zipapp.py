"""Verification for the `just build` zipapp artifact (tools/build_zipapp.py).

Covers the contract a consumer relies on: a single `./microflows-viz` file that
runs on Python 3.10+ with no virtualenv and no package install — reporting the
right version, showing help, bundling PyMySQL, and rebuilding cleanly.

Modeled directly on ../mariachi's packaging suite (via the retired mfinspect tool's).
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = REPO_ROOT / "tools" / "build_zipapp.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
COMMITTED_ARTIFACT = REPO_ROOT / "microflows-viz"

# Import the build module in-process so a failure mid-build can be simulated.
sys.path.insert(0, str(REPO_ROOT / "tools"))
import build_zipapp  # noqa: E402


def _sha256(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def _pyproject_version() -> str:
	text = PYPROJECT.read_text(encoding="utf-8")
	match = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
	assert match, "version not found in pyproject.toml"
	return match.group(1)


def _clean_env() -> dict:
	"""Environment with nothing that could leak the source tree or venv onto the
	import path — proves the artifact is genuinely self-contained."""
	env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
	return env


def _build(output: Path) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(BUILD_SCRIPT), "-o", str(output)],
		cwd=REPO_ROOT,
		capture_output=True,
		text=True,
	)


@unittest.skipUnless(
	(REPO_ROOT / ".venv").is_dir(),
	"zipapp build needs the project .venv (run `just setup`)",
)
class ZipappBuildTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		cls._tmp = tempfile.TemporaryDirectory(prefix="microflows-viz-zipapp-test-")
		cls.tmp_path = Path(cls._tmp.name)
		cls.artifact = cls.tmp_path / "microflows-viz"
		result = _build(cls.artifact)
		if result.returncode != 0:
			raise AssertionError(f"build failed:\n{result.stdout}\n{result.stderr}")
		cls.version = _pyproject_version()

	@classmethod
	def tearDownClass(cls) -> None:
		cls._tmp.cleanup()

	def _run_artifact(self, *args: str) -> subprocess.CompletedProcess:
		# Run from a directory outside the repo so `src/` can't shadow the bundle,
		# and with a cleaned env (no PYTHONPATH / no venv) — a true standalone run.
		return subprocess.run(
			[str(self.artifact), *args],
			cwd=self.tmp_path,
			env=_clean_env(),
			capture_output=True,
			text=True,
		)

	def test_artifact_is_executable_with_shebang(self) -> None:
		self.assertTrue(os.access(self.artifact, os.X_OK), "artifact is not executable")
		with self.artifact.open("rb") as handle:
			first_line = handle.readline()
		self.assertEqual(first_line, b"#!/usr/bin/env python3\n")

	def test_version_reports_pyproject_version(self) -> None:
		result = self._run_artifact("--version")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout.strip(), f"microflows-viz {self.version}")

	def test_help_runs_standalone(self) -> None:
		result = self._run_artifact("--help")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("usage: microflows-viz", result.stdout)

	def test_serve_help_runs_standalone(self) -> None:
		result = self._run_artifact("serve", "--help")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("--db-host", result.stdout)
		self.assertIn("--listen", result.stdout)

	def test_runs_without_venv_or_pythonpath(self) -> None:
		# The cleaned env in _run_artifact has no PYTHONPATH and no VIRTUAL_ENV;
		# a successful --version proves the bundle needs neither.
		self.assertNotIn("PYTHONPATH", _clean_env())
		result = self._run_artifact("--version")
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_pymysql_is_embedded_and_importable(self) -> None:
		names = zipfile.ZipFile(self.artifact).namelist()
		self.assertIn("pymysql/__init__.py", names)
		# Import it straight out of the zip, with the source tree off the path.
		probe = subprocess.run(
			[sys.executable, "-c", "import pymysql; print(pymysql.__file__)"],
			cwd=self.tmp_path,
			env={**_clean_env(), "PYTHONPATH": str(self.artifact)},
			capture_output=True,
			text=True,
		)
		self.assertEqual(probe.returncode, 0, probe.stderr)
		self.assertIn(f"microflows-viz{os.sep}pymysql", probe.stdout)

	def test_metadata_is_bundled(self) -> None:
		names = zipfile.ZipFile(self.artifact).namelist()
		self.assertIn(f"microflows_viz-{self.version}.dist-info/METADATA", names)

	def test_static_ui_is_not_bundled(self) -> None:
		# The UI is served from the artifact's own directory, not from inside the
		# zip (vendor/mermaid.min.js alone is ~3.5 MB) — see cli._resolve_static_root.
		names = zipfile.ZipFile(self.artifact).namelist()
		self.assertFalse([n for n in names if n.endswith(".html")], "UI html leaked into the zip")
		self.assertFalse([n for n in names if n.startswith("vendor/")], "vendor/ leaked into the zip")

	def test_pymysql_license_is_bundled(self) -> None:
		# PyMySQL is MIT-licensed; the embedded copy must carry its license.
		archive = zipfile.ZipFile(self.artifact)
		names = archive.namelist()
		license_entries = [
			n
			for n in names
			if n.lower().startswith("pymysql-")
			and "license" in n.rsplit("/", 1)[-1].lower()
		]
		self.assertTrue(
			license_entries,
			f"no PyMySQL license bundled; archive has: {sorted(names)[:20]}",
		)
		text = archive.read(license_entries[0]).decode("utf-8")
		self.assertIn("Permission is hereby granted", text)  # the MIT grant
		# And a dist-info METADATA naming the license, so attribution is complete.
		metadata_entries = [
			n
			for n in names
			if n.lower().startswith("pymysql-") and n.lower().endswith("/metadata")
		]
		self.assertTrue(metadata_entries, "no PyMySQL dist-info METADATA bundled")
		metadata = archive.read(metadata_entries[0]).decode("utf-8")
		self.assertIn("Name: PyMySQL", metadata)
		self.assertIn("MIT", metadata)

	def test_build_is_reproducible(self) -> None:
		# Two fresh builds of identical inputs must be byte-for-byte identical.
		first = self.tmp_path / "repro_a"
		second = self.tmp_path / "repro_b"
		self.assertEqual(_build(first).returncode, 0)
		self.assertEqual(_build(second).returncode, 0)
		self.assertEqual(
			_sha256(first),
			_sha256(second),
			"two unchanged builds differ — archive is not reproducible",
		)

	def test_excludes_caches_bytecode_and_tests(self) -> None:
		names = zipfile.ZipFile(self.artifact).namelist()
		self.assertFalse([n for n in names if "__pycache__" in n], "bytecode cache leaked in")
		self.assertFalse([n for n in names if n.endswith(".pyc")], ".pyc leaked in")
		self.assertFalse([n for n in names if n.startswith("tests/")], "tests leaked in")

	def test_clean_rebuild_replaces_stale_target(self) -> None:
		out = self.tmp_path / "rebuilt"
		# A pre-existing, unrelated file at the target must be cleanly replaced.
		out.write_text("stale garbage", encoding="utf-8")
		first = _build(out)
		self.assertEqual(first.returncode, 0, first.stderr)
		self.assertTrue(zipfile.is_zipfile(out), "stale file was not replaced by a zipapp")
		# Rebuilding again produces an equivalent, working executable.
		second = _build(out)
		self.assertEqual(second.returncode, 0, second.stderr)
		self.assertTrue(os.access(out, os.X_OK))
		check = subprocess.run(
			[str(out), "--version"], cwd=self.tmp_path, env=_clean_env(),
			capture_output=True, text=True,
		)
		self.assertEqual(check.stdout.strip(), f"microflows-viz {self.version}")

	def test_failed_build_preserves_existing_artifact(self) -> None:
		# A build that fails mid-flight must leave any existing artifact untouched
		# (it stages into a sibling temp file and only os.replace()s on success).
		out = self.tmp_path / "preserved"
		self.assertEqual(_build(out).returncode, 0)
		original_bytes = out.read_bytes()
		before = sorted(p.name for p in out.parent.iterdir())

		with mock.patch.object(
			build_zipapp, "_write_archive", side_effect=RuntimeError("boom")
		):
			with self.assertRaises(RuntimeError):
				build_zipapp.build(out)

		# The good artifact is byte-for-byte intact and still executable...
		self.assertEqual(out.read_bytes(), original_bytes, "artifact was clobbered by a failed build")
		self.assertTrue(os.access(out, os.X_OK))
		# ...and no half-written temp file was left behind.
		after = sorted(p.name for p in out.parent.iterdir())
		self.assertEqual(before, after, "a leftover temp file survived the failed build")

	def test_failed_build_without_existing_artifact_creates_none(self) -> None:
		# Same failure path when there is no prior artifact: nothing is created.
		out = self.tmp_path / "never_created"
		with mock.patch.object(
			build_zipapp, "_write_archive", side_effect=RuntimeError("boom")
		):
			with self.assertRaises(RuntimeError):
				build_zipapp.build(out)
		self.assertFalse(out.exists())
		leftovers = [p.name for p in out.parent.iterdir() if p.name.startswith(".microflows-viz-zipapp-")]
		self.assertEqual(leftovers, [], "a leftover temp file survived the failed build")


@unittest.skipUnless(
	(REPO_ROOT / ".venv").is_dir(),
	"committed-artifact check needs the project .venv (run `just setup`)",
)
class CommittedArtifactTests(unittest.TestCase):
	"""Guards against the committed ./microflows-viz drifting from source. Because
	builds are reproducible, a stale binary is caught by a byte-for-byte hash
	comparison."""

	def test_committed_artifact_exists_and_is_executable(self) -> None:
		self.assertTrue(
			COMMITTED_ARTIFACT.is_file(),
			"no committed ./microflows-viz — run `just setup && just build` and commit it",
		)
		self.assertTrue(os.access(COMMITTED_ARTIFACT, os.X_OK))

	def test_committed_artifact_matches_fresh_build(self) -> None:
		if not COMMITTED_ARTIFACT.is_file():
			self.skipTest("no committed ./microflows-viz artifact")
		with tempfile.TemporaryDirectory(prefix="microflows-viz-committed-") as tmp:
			fresh = Path(tmp) / "microflows-viz"
			result = _build(fresh)
			self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
			self.assertEqual(
				_sha256(COMMITTED_ARTIFACT),
				_sha256(fresh),
				"committed ./microflows-viz is stale — rebuild with `just setup && just build` "
				"and commit the result",
			)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
