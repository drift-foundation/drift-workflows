#!/usr/bin/env python3
"""Build a self-contained `mfinspect` executable with the standard-library zipapp.

The artifact bundles, into one zip with a `#!/usr/bin/env python3` shebang:

  * the `mfinspect` package source (from `src/`),
  * the PyMySQL dependency (copied from the local `.venv`) **with its MIT license
    and a minimal dist-info**, and
  * a synthetic `mfinspect-<version>.dist-info` so `importlib.metadata` — and
    therefore `mfinspect --version` — reports the `pyproject.toml` version from
    inside the zip.

The result runs on any machine with Python 3.10+: no virtualenv, no pip install.
Invoke it through `just build` (which ensures the venv exists first). Stdlib only,
so it also runs under a bare `python3 tools/build_zipapp.py`.

Builds are byte-for-byte reproducible: archive entries are written in sorted order
with a fixed timestamp and fixed permissions, so two builds of identical inputs
produce identical SHA-256 hashes. The committed `./mfinspect` is therefore verifiable
against a fresh build (see tests/test_zipapp.py).

Modeled directly on ../../../../mariachi/tools/build_zipapp.py — keep the two in sync
if the packaging contract changes.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_PKG = REPO_ROOT / "src" / "mfinspect"
VENV = REPO_ROOT / ".venv"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DEFAULT_ARTIFACT = REPO_ROOT / "mfinspect"
INTERPRETER = "/usr/bin/env python3"

# A fixed DOS timestamp (the ZIP epoch, 1980-01-01) so the archive does not carry
# build-time mtimes — the key to reproducible output.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# Never copied into the archive: caches, bytecode, vendored test trees.
_EXCLUDE_DIRS = {"__pycache__", "tests", "test"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd")

# Filenames (case-insensitive prefix) treated as license/notice files to preserve.
_LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


def fail(message: str):
	print(f"build_zipapp: error: {message}", file=sys.stderr)
	raise SystemExit(1)


def read_version() -> str:
	"""Authoritative version from pyproject.toml. Parsed with a regex rather than
	tomllib so the script also runs on Python 3.10 (tomllib landed in 3.11)."""
	if not PYPROJECT.is_file():
		fail(f"pyproject.toml not found at {PYPROJECT}")
	text = PYPROJECT.read_text(encoding="utf-8")
	match = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
	if not match:
		fail('could not find `version = "..."` in pyproject.toml')
	return match.group(1)


def find_pymysql() -> tuple[Path, Path | None]:
	"""Locate PyMySQL (and its dist-info) inside the project's .venv. Fails clearly
	when the venv or the dependency is missing, pointing the user at `just setup`."""
	if not VENV.is_dir():
		fail(f".venv not found at {VENV} — run `just setup` first")
	candidates = sorted(VENV.glob("lib/python*/site-packages/pymysql"))
	candidates += sorted(VENV.glob("Lib/site-packages/pymysql"))  # Windows layout
	for candidate in candidates:
		if (candidate / "__init__.py").is_file():
			site_packages = candidate.parent
			dist_infos = sorted(site_packages.glob("[pP]y[mM]y[sS][qQ][lL]-*.dist-info"))
			return candidate, (dist_infos[0] if dist_infos else None)
	fail("PyMySQL not found in .venv — run `just setup` to install dependencies")


def _is_license_file(name: str) -> bool:
	upper = name.upper()
	return any(upper.startswith(prefix) for prefix in _LICENSE_PREFIXES)


def _pymysql_version(dist_info: Path | None) -> str:
	if dist_info is not None:
		match = re.match(r"pymysql-(.+)\.dist-info$", dist_info.name, re.IGNORECASE)
		if match:
			return match.group(1)
	return "unknown"


def _collect_license_files(dist_info: Path | None) -> list[Path]:
	if dist_info is None:
		return []
	return sorted(
		path
		for path in dist_info.rglob("*")
		if path.is_file() and _is_license_file(path.name)
	)


def _ignore(_dir: str, names: list[str]) -> set[str]:
	return {
		name
		for name in names
		if name in _EXCLUDE_DIRS or name.endswith(_EXCLUDE_SUFFIXES)
	}


def stage(staging: Path, version: str, pymysql_dir: Path, pymysql_dist_info: Path | None) -> None:
	# 1. The mfinspect package source.
	shutil.copytree(SRC_PKG, staging / "mfinspect", ignore=_ignore)
	# 2. The PyMySQL dependency from the active venv.
	shutil.copytree(pymysql_dir, staging / "pymysql", ignore=_ignore)
	# 3. PyMySQL attribution: bundle its license + a minimal dist-info so the MIT
	#    license travels with the embedded copy (and its version is recorded).
	py_version = _pymysql_version(pymysql_dist_info)
	licenses = _collect_license_files(pymysql_dist_info)
	if not licenses:
		fail(
			"PyMySQL license file not found in its dist-info — refusing to bundle "
			"the dependency without its MIT license."
		)
	py_dist = staging / f"pymysql-{py_version}.dist-info"
	py_dist.mkdir()
	license_lines = ""
	for lic in licenses:
		(py_dist / lic.name).write_bytes(lic.read_bytes())
		license_lines += f"License-File: {lic.name}\n"
	(py_dist / "METADATA").write_text(
		"Metadata-Version: 2.1\n"
		"Name: PyMySQL\n"
		f"Version: {py_version}\n"
		"License-Expression: MIT\n"
		+ license_lines,
		encoding="utf-8",
	)
	# 4. A synthetic mfinspect dist-info so importlib.metadata.version("mfinspect")
	#    resolves inside the zip and reports the pyproject version (drives `--version`).
	dist_info = staging / f"mfinspect-{version}.dist-info"
	dist_info.mkdir()
	(dist_info / "METADATA").write_text(
		"Metadata-Version: 2.1\n"
		"Name: mfinspect\n"
		f"Version: {version}\n"
		"Summary: Read-only workflow/call-tree state dump for Microflows composition (1b.1/1c).\n"
		"Requires-Python: >=3.10\n",
		encoding="utf-8",
	)
	# 5. The zipapp entry point — delegates to the package's existing run().
	(staging / "__main__.py").write_text(
		"from mfinspect.mfinspect import run\n\n"
		'if __name__ == "__main__":\n'
		"	run()\n",
		encoding="utf-8",
	)


def _write_archive(staging: Path, artifact: Path) -> None:
	"""Write a byte-deterministic zipapp: a shebang followed by a ZIP whose entries
	are sorted, stamped with a fixed timestamp, and given fixed permissions. Two
	builds of identical inputs are therefore SHA-256 identical."""
	files = sorted(
		(path for path in staging.rglob("*") if path.is_file()),
		key=lambda path: path.relative_to(staging).as_posix(),
	)
	with artifact.open("wb") as handle:
		handle.write(b"#!" + INTERPRETER.encode("utf-8") + b"\n")
		with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
			for path in files:
				arcname = path.relative_to(staging).as_posix()
				info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
				info.compress_type = zipfile.ZIP_DEFLATED
				info.external_attr = (stat.S_IFREG | 0o644) << 16  # regular, rw-r--r--
				archive.writestr(info, path.read_bytes())


def build(artifact: Path) -> str:
	if not SRC_PKG.is_dir():
		fail(f"package source not found at {SRC_PKG}")
	version = read_version()
	pymysql_dir, pymysql_dist_info = find_pymysql()
	artifact.parent.mkdir(parents=True, exist_ok=True)

	with tempfile.TemporaryDirectory(prefix="mfinspect-zipapp-") as tmp:
		staging = Path(tmp) / "app"
		staging.mkdir()
		# Stage first: a failure here (e.g. a missing dependency license) must not
		# touch any existing artifact.
		stage(staging, version, pymysql_dir, pymysql_dist_info)
		# Build into a sibling temp file on the target's filesystem, then swap it
		# into place atomically. An existing `./mfinspect` survives any failure
		# during archiving, and a concurrent reader never sees a half-written file.
		fd, tmp_name = tempfile.mkstemp(prefix=".mfinspect-zipapp-", dir=artifact.parent)
		os.close(fd)
		pending = Path(tmp_name)
		try:
			_write_archive(staging, pending)
			pending.chmod(0o755)  # explicit, umask-independent: rwxr-xr-x
			os.replace(pending, artifact)
		except BaseException:
			pending.unlink(missing_ok=True)
			raise
	return version


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument(
		"-o",
		"--output",
		type=Path,
		default=DEFAULT_ARTIFACT,
		help="Path for the generated executable (default: ./mfinspect at repo root).",
	)
	args = parser.parse_args(argv)
	version = build(args.output)
	try:
		shown = args.output.resolve().relative_to(REPO_ROOT)
	except ValueError:
		shown = args.output
	print(f"Built {shown} (mfinspect {version}) — run it with Python 3.10+, no venv needed.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
