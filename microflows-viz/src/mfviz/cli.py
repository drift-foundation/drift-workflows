"""microflows-viz CLI — `serve` runs the operator-facing backend.

  microflows-viz serve [--listen HOST:PORT] [--allow-remote] [--static-root DIR]
      [--db-host H] [--db-port P] [--db-user U]
      [--db-password PW | --db-password-env VAR] [--db-name NAME]

DB connection flags are deliberately `--db-`-prefixed (accepted decision,
work/viz-consolidation): on an operator-facing serve command a bare
`--host`/`--port` must never silently mean "database" — the HTTP bind is
`--listen`, the database is `--db-*`. Env defaults match mariachi's convention
(DB_HOST/DB_PORT/DB_USER/DB_NAME/MDB_ROOT_PWD), and --db-password-env is
preferred over --db-password so a secret never lands in shell history or a
process listing.

Binds 127.0.0.1 by default; a non-loopback --listen requires --allow-remote as
an explicit opt-in (the API exposes full workflow state — args, payloads,
events — to whoever can reach the port).

Read-only by permissions: run with `--db-user viz_ro --db-password-env
VIZ_RO_PWD` (grant: microflows/db/grants/viz_ro.sql). Root/dev credentials
remain possible for local convenience.
"""
from __future__ import annotations

import argparse
import os
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Iterable

from . import dbq, server

try:
	# pyproject.toml is the authoritative version; read it from installed metadata so the two
	# never drift. Falls back when running from an uninstalled checkout.
	__version__ = _pkg_version("microflows-viz")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
	__version__ = "0.0.0+source"

DEFAULT_LISTEN = "127.0.0.1:8377"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _resolve_password(args: argparse.Namespace) -> str:
	if args.db_password is not None:
		return args.db_password
	try:
		return os.environ[args.db_password_env]
	except KeyError as exc:
		raise dbq.MfvizError(
			f"environment variable {args.db_password_env} is not set "
			"(or pass --db-password directly)"
		) from exc


def _parse_listen(value: str) -> tuple[str, int]:
	host, sep, port_text = value.rpartition(":")
	if not sep or not host:
		raise dbq.MfvizError(f"--listen must be HOST:PORT, got {value!r}")
	try:
		port = int(port_text)
	except ValueError as exc:
		raise dbq.MfvizError(f"--listen port is not an integer: {value!r}") from exc
	if not 0 <= port <= 65535:
		raise dbq.MfvizError(f"--listen port out of range: {value!r}")
	return host.strip("[]"), port


def _resolve_static_root(explicit: str | None) -> Path:
	"""The UI files are NOT bundled into the zipapp (vendor/mermaid.min.js alone is
	~3.5 MB); the committed executable lives next to index.html in microflows-viz/,
	so the default is the executable's own directory, then the cwd."""
	if explicit is not None:
		return Path(explicit)
	candidates = [Path(sys.argv[0]).resolve().parent, Path.cwd()]
	for candidate in candidates:
		if (candidate / "index.html").is_file():
			return candidate
	raise dbq.MfvizError(
		"cannot locate the static UI (no index.html next to the executable or in the "
		"current directory) — pass --static-root"
	)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog="microflows-viz",
		description="Operator-facing viewer for Microflows workflow state. The `serve` backend "
		            "owns all DB access (read-only; run it as the SELECT-only viz_ro user) and "
		            "serves the browser UI + JSON /api on one origin.")
	parser.add_argument(
		"--version", "-v", action="version", version=f"microflows-viz {__version__}",
		help="Print the microflows-viz version and exit.",
	)

	subparsers = parser.add_subparsers(dest="command", required=True)

	serve_parser = subparsers.add_parser(
		"serve", help="Serve the static UI and the read-only JSON /api over the coordinator DB.")
	serve_parser.add_argument(
		"--listen", default=DEFAULT_LISTEN,
		help=f"HOST:PORT for the HTTP server (default: {DEFAULT_LISTEN}). A non-loopback host "
		     "additionally requires --allow-remote.",
	)
	serve_parser.add_argument(
		"--allow-remote", action="store_true",
		help="Explicitly allow binding a non-loopback --listen host (the API exposes full "
		     "workflow state to whoever can reach the port).",
	)
	serve_parser.add_argument(
		"--static-root", default=None,
		help="Directory holding the UI files (index.html, vendor/, ...). Default: the "
		     "executable's own directory, then the current directory.",
	)
	serve_parser.add_argument(
		"--db-host", default=os.environ.get("DB_HOST", "127.0.0.1"),
		help="MariaDB host to connect to (default: $DB_HOST or 127.0.0.1).",
	)
	serve_parser.add_argument(
		"--db-port", type=int, default=int(os.environ.get("DB_PORT", "34214")),
		help="MariaDB port to connect to (default: $DB_PORT or 34214).",
	)
	serve_parser.add_argument(
		"--db-user", default=os.environ.get("DB_USER", "root"),
		help="MariaDB user to authenticate as (default: $DB_USER or root). Production posture "
		     "is the SELECT-only viz_ro user (microflows/db/grants/viz_ro.sql).",
	)
	serve_parser.add_argument(
		"--db-password", default=None,
		help="MariaDB password, given directly. Mostly for local dev; prefer --db-password-env "
		     "so the password never appears in shell history or a process listing.",
	)
	serve_parser.add_argument(
		"--db-password-env", default="MDB_ROOT_PWD",
		help="Environment variable that holds the MariaDB password, used unless --db-password "
		     "is given directly (default: MDB_ROOT_PWD).",
	)
	serve_parser.add_argument(
		"--db-name", default=os.environ.get("DB_NAME", "microflows"),
		help="MariaDB schema/database name (default: $DB_NAME or microflows).",
	)
	serve_parser.set_defaults(func=cmd_serve)

	return parser


def cmd_serve(args: argparse.Namespace) -> int:
	host, port = _parse_listen(args.listen)
	if host not in _LOOPBACK_HOSTS and not args.allow_remote:
		raise dbq.MfvizError(
			f"refusing non-loopback --listen {args.listen!r} without --allow-remote"
		)
	db_cfg = dbq.DbConfig(
		host=args.db_host, port=args.db_port, user=args.db_user,
		password=_resolve_password(args), database=args.db_name,
	)
	static_root = _resolve_static_root(args.static_root).resolve()
	# Startup probe: a non-UTC coordinator DB is a configuration error — fail
	# fast here rather than 502 on every request (connect() also enforces this
	# per-request, so a DB that changes underneath us still fails closed). A DB
	# that is merely unreachable stays non-fatal: endpoints 502 until it is up.
	try:
		dbq.connect(db_cfg).close()
	except dbq.DbNotUtcError as exc:
		raise dbq.MfvizError(str(exc)) from exc
	except Exception as exc:
		print(f"microflows-viz: warning: DB not reachable at startup ({exc}); "
		      "endpoints will return 502 until it is", file=sys.stderr)
	httpd = server.create_server(host, port, db_cfg, static_root)
	bound_host, bound_port = httpd.server_address[0], httpd.server_address[1]
	print(
		f"microflows-viz {__version__}: serving http://{bound_host}:{bound_port} "
		f"(static: {static_root}; db: {db_cfg.user}@{db_cfg.host}:{db_cfg.port}/{db_cfg.database})",
		file=sys.stderr,
	)
	try:
		httpd.serve_forever()
	except KeyboardInterrupt:
		print("microflows-viz: shutting down", file=sys.stderr)
	finally:
		httpd.server_close()
	return 0


def main(argv: Iterable[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(list(argv) if argv is not None else None)
	try:
		return args.func(args)
	except dbq.MfvizError as exc:
		print(f"microflows-viz error: {exc}", file=sys.stderr)
		return 1


def run() -> None:
	sys.exit(main())


if __name__ == "__main__":
	run()
