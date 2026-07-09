"""stdlib HTTP server for `microflows-viz serve` — static UI + read-only /api.

Accepted decisions (work/viz-consolidation):
  * Python stdlib HTTP serving primitives only (http.server); no web framework.
  * The backend owns ALL DB access; the browser talks only to /api on this origin
    and never sees a DB host, port, or credential.
  * Read-only enforced by permissions: run as the SELECT-only `viz_ro` user
    (microflows/db/grants/viz_ro.sql). Every query in mfviz.dbq is a SELECT.

Static serving is deliberately allowlist-based: only the known UI entries
(index.html, scenarios.js, microflows.machine.js, vendor/, plans/) resolve, so
the server can never be talked into shipping pyproject.toml, src/, .venv/, or
anything else that happens to live next to the UI files. Paths are resolved and
prefix-checked against the static root as a second, independent guard.

API (slice 1):
  GET /api/health                          -> {"ok": true, "database": ...}
  GET /api/workflow/<32-hex>?max_depth=N   -> mfinspect-`inspect`-parity JSON tree
  GET /api/workflows?script=&since=&until= -> mfinspect-`list`-parity summary array
      [&plan_version=][&state=]               (script+since+until REQUIRED -> 400,
                                                the bounded-scan rule; each entry
                                                carries an `href` to its
                                                /api/workflow/<id> inspection)
"""
from __future__ import annotations

import json
import posixpath
import re
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dbq

STATIC_ALLOWLIST = {"index.html", "scenarios.js", "microflows.machine.js", "vendor", "plans"}

CONTENT_TYPES = {
	".html": "text/html; charset=utf-8",
	".js": "text/javascript; charset=utf-8",
	".css": "text/css; charset=utf-8",
	".json": "application/json; charset=utf-8",
	".mf": "text/plain; charset=utf-8",
	".svg": "image/svg+xml",
	".png": "image/png",
	".ico": "image/x-icon",
}

_WORKFLOW_ROUTE = re.compile(r"/api/workflow/([0-9a-fA-F]{32})")


class VizServer(ThreadingHTTPServer):
	daemon_threads = True

	def __init__(self, address: tuple[str, int], db_cfg: dbq.DbConfig, static_root: Path):
		self.db_cfg = db_cfg
		self.static_root = static_root.resolve()
		super().__init__(address, VizRequestHandler)


class VizRequestHandler(BaseHTTPRequestHandler):
	server: VizServer  # narrowed for type checkers; set by http.server machinery

	def log_message(self, fmt: str, *args) -> None:  # concise one-line access log
		sys.stderr.write("microflows-viz: %s %s\n" % (self.address_string(), fmt % args))

	# ===== responses =====

	def _send_json(self, status: int, payload) -> None:
		body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(body)

	def _send_file(self, path: Path) -> None:
		body = path.read_bytes()
		self.send_response(HTTPStatus.OK)
		self.send_header(
			"Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	# ===== routing =====

	def do_GET(self) -> None:
		try:
			parsed = urllib.parse.urlparse(self.path)
			if parsed.path == "/api" or parsed.path.startswith("/api/"):
				self._handle_api(parsed)
			else:
				self._handle_static(parsed.path)
		except BrokenPipeError:  # client went away mid-response; nothing to salvage
			pass
		except Exception as exc:  # never leak a traceback as a raw 500 page
			try:
				self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
				                {"error": "internal_error", "detail": str(exc)})
			except Exception:
				pass

	def do_HEAD(self) -> None:  # pragma: no cover - politeness, not part of the API contract
		self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
		self.end_headers()

	def do_POST(self) -> None:
		self._method_not_allowed()

	def do_PUT(self) -> None:
		self._method_not_allowed()

	def do_DELETE(self) -> None:
		self._method_not_allowed()

	def _method_not_allowed(self) -> None:
		# Read-only surface: the API has no mutating verbs at all.
		self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

	# ===== /api =====

	def _handle_api(self, parsed) -> None:
		if parsed.path == "/api/health":
			self._api_health()
			return
		if parsed.path == "/api/workflows":
			self._api_workflows(parsed.query)
			return
		match = _WORKFLOW_ROUTE.fullmatch(parsed.path)
		if match:
			self._api_workflow(match.group(1), parsed.query)
			return
		self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": parsed.path})

	def _api_health(self) -> None:
		try:
			conn = dbq.connect(self.server.db_cfg)
			try:
				with conn.cursor() as c:
					c.execute("SELECT 1")
					c.fetchone()
			finally:
				conn.close()
		except Exception as exc:
			self._send_json(HTTPStatus.BAD_GATEWAY,
			                {"ok": False, "error": "db_unreachable", "detail": str(exc)})
			return
		self._send_json(HTTPStatus.OK, {"ok": True, "database": self.server.db_cfg.database})

	def _api_workflow(self, workflow_id_hex: str, query: str) -> None:
		params = urllib.parse.parse_qs(query)
		raw_depth = params.get("max_depth", [str(dbq.DEFAULT_MAX_DEPTH)])[-1]
		try:
			max_depth = int(raw_depth)
		except ValueError:
			self._send_json(HTTPStatus.BAD_REQUEST,
			                {"error": "bad_request", "detail": f"max_depth is not an integer: {raw_depth!r}"})
			return
		if max_depth < 1:
			self._send_json(HTTPStatus.BAD_REQUEST,
			                {"error": "bad_request", "detail": "max_depth must be >= 1"})
			return
		try:
			conn = dbq.connect(self.server.db_cfg)
			try:
				node = dbq.inspect_workflow(conn, workflow_id_hex, max_depth)
			finally:
				conn.close()
		except dbq.MfvizError as exc:
			self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
			return
		except Exception as exc:
			self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "db_error", "detail": str(exc)})
			return
		if node.get("error") == "not_found":
			self._send_json(HTTPStatus.NOT_FOUND, node)
			return
		self._send_json(HTTPStatus.OK, node)

	def _api_workflows(self, query: str) -> None:
		params = urllib.parse.parse_qs(query)

		def one(name: str) -> str | None:
			values = params.get(name)
			if not values or not values[-1]:
				return None
			return values[-1]

		script, since, until = one("script"), one("since"), one("until")
		missing = [name for name, value in
		           (("script", script), ("since", since), ("until", until)) if value is None]
		if missing:
			# The bounded-scan rule, enforced structurally (mfinspect `list` parity):
			# an unbounded search of tb_mf_workflow is never one typo away.
			self._send_json(HTTPStatus.BAD_REQUEST, {
				"error": "bad_request",
				"detail": "missing required query parameter(s): " + ", ".join(missing)
				          + " (script + a bounded since/until created_at range are required)",
			})
			return
		try:
			conn = dbq.connect(self.server.db_cfg)
			try:
				summaries = dbq.list_workflows(
					conn, script, since, until,
					plan_version=one("plan_version"), state=one("state"))
			finally:
				conn.close()
		except dbq.MfvizError as exc:
			self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "detail": str(exc)})
			return
		except Exception as exc:
			self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "db_error", "detail": str(exc)})
			return
		for entry in summaries:
			entry["href"] = f"/api/workflow/{entry['workflow_id']}"
		self._send_json(HTTPStatus.OK, summaries)

	# ===== static UI =====

	def _handle_static(self, raw_path: str) -> None:
		# Normalize BEFORE the allowlist check, else "/vendor/../pyproject.toml"
		# passes the allowlist under "vendor" and resolves back inside the root.
		rel = posixpath.normpath(urllib.parse.unquote(raw_path).lstrip("/"))
		if rel in ("", "."):
			rel = "index.html"
		# Guard 1: no ".." survives normalization except leading ones — reject all.
		if any(part == ".." for part in rel.split("/")):
			self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": raw_path})
			return
		# Guard 2: only allowlisted top-level entries are servable at all.
		first = rel.split("/", 1)[0]
		if first not in STATIC_ALLOWLIST:
			self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": raw_path})
			return
		# Guard 3: the resolved target must still live under the static root
		# (catches symlink escapes independently of the guards above).
		root = self.server.static_root
		target = (root / rel).resolve()
		if root != target and root not in target.parents:
			self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": raw_path})
			return
		if not target.is_file():
			self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": raw_path})
			return
		self._send_file(target)


def create_server(listen_host: str, listen_port: int, db_cfg: dbq.DbConfig,
                  static_root: Path) -> VizServer:
	if not (static_root / "index.html").is_file():
		raise dbq.MfvizError(
			f"static root {static_root} does not contain index.html "
			"(pass --static-root pointing at the microflows-viz UI directory)"
		)
	return VizServer((listen_host, listen_port), db_cfg, static_root)
