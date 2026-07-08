"""DB-backed tests for `microflows-viz serve`: the read-only grant, the static
allowlist, and the first API endpoint (GET /api/workflow/<id_hex>).

These tests run against the standing dev fixture DB (DB_HOST/DB_PORT, default
127.0.0.1:34214, database `microflows`) and SKIP cleanly when it is unreachable
or the microflows schema is not loaded — same posture as the repo's other
fixture-backed suites. That skip is a LOCAL convenience only: a certification
gate must export MFVIZ_REQUIRE_DB=1, which turns DB/schema absence into a hard
import-time failure — the read-only-grant proof is the point of this suite and
must never silently skip in a gate.

The read-only guarantee is tested BY PERMISSION, per the work/viz-consolidation
charter: setUpClass applies the actual grant file
(microflows/db/grants/viz_ro.sql, {{SCHEMA}} substituted, exactly as Mariachi
would) through the root fixture credentials, then every server test runs the
backend as `viz_ro` — and a direct INSERT/UPDATE/DELETE as that user must be
denied by MariaDB itself (ER_TABLEACCESS_DENIED, 1142).
"""
import http.client
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import pymysql

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from mfviz import dbq, server  # noqa: E402

GRANT_FILE = REPO_ROOT.parent / "microflows" / "db" / "grants" / "viz_ro.sql"

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "34214"))
DB_NAME = os.environ.get("DB_NAME", "microflows")
ROOT_USER = os.environ.get("DB_USER", "root")
ROOT_PWD = os.environ.get("MDB_ROOT_PWD", "rootpw")
VIZ_RO_USER = "viz_ro"
VIZ_RO_PWD = os.environ.get("VIZ_RO_PWD", "vizro_dev")

MISSING_ID = "0" * 32  # syntactically valid, never assigned (uuids are random)


def _db_reachable() -> bool:
	try:
		with socket.create_connection((DB_HOST, DB_PORT), timeout=1.0):
			return True
	except OSError:
		return False


def _root_connect(database=None):
	return pymysql.connect(host=DB_HOST, port=DB_PORT, user=ROOT_USER, password=ROOT_PWD,
	                       database=database, autocommit=True)


def _grant_statements() -> list[str]:
	"""The grant file's executable statements, {{SCHEMA}} substituted — the same
	text Mariachi would run, so the test exercises the committed file itself."""
	lines = [
		line for line in GRANT_FILE.read_text(encoding="utf-8").splitlines()
		if not line.lstrip().startswith("--")
	]
	return [s.strip() for s in "\n".join(lines).replace("{{SCHEMA}}", DB_NAME).split(";")
	        if s.strip()]


def _schema_loaded() -> bool:
	try:
		conn = _root_connect()
	except pymysql.MySQLError:
		return False
	try:
		with conn.cursor() as c:
			c.execute(
				"SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
				"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'tb_mf_workflow' LIMIT 1",
				(DB_NAME,),
			)
			return c.fetchone() is not None
	finally:
		conn.close()


_DB_UP = _db_reachable()
_SCHEMA_UP = _DB_UP and _schema_loaded()

# Gate strictness: with MFVIZ_REQUIRE_DB=1 (set by repo-gate wiring), an absent
# fixture DB is a loud failure, not a skip — the grant proof must actually run.
if os.environ.get("MFVIZ_REQUIRE_DB", "0") not in ("", "0") and not _SCHEMA_UP:
	raise RuntimeError(
		f"MFVIZ_REQUIRE_DB is set, but the microflows fixture DB is unavailable "
		f"(host={DB_HOST} port={DB_PORT} reachable={_DB_UP}, "
		f"schema {DB_NAME!r} loaded={_SCHEMA_UP}) — the gate must provision the DB "
		"before running this suite; refusing to skip the read-only-grant proof."
	)


@unittest.skipUnless(_SCHEMA_UP, f"microflows fixture DB not available at {DB_HOST}:{DB_PORT}")
class VizRoGrantTests(unittest.TestCase):
	"""The committed grant file yields a user that can read everything and write nothing."""

	@classmethod
	def setUpClass(cls) -> None:
		conn = _root_connect()
		try:
			with conn.cursor() as c:
				for statement in _grant_statements():
					c.execute(statement)
		finally:
			conn.close()

	def _viz_conn(self):
		return pymysql.connect(host=DB_HOST, port=DB_PORT, user=VIZ_RO_USER,
		                       password=VIZ_RO_PWD, database=DB_NAME, autocommit=True)

	def test_select_is_allowed(self) -> None:
		conn = self._viz_conn()
		try:
			with conn.cursor() as c:
				c.execute("SELECT COUNT(*) FROM tb_mf_workflow")
				row = c.fetchone()
			self.assertIsNotNone(row)
		finally:
			conn.close()

	def test_writes_are_denied_by_the_grant(self) -> None:
		conn = self._viz_conn()
		try:
			denied = []
			for label, statement in (
				("insert", "INSERT INTO tb_mf_workflow (workflow_id) VALUES (%s)"),
				("update", "UPDATE tb_mf_workflow SET state = 1 WHERE workflow_id = %s"),
				("delete", "DELETE FROM tb_mf_workflow WHERE workflow_id = %s"),
			):
				with conn.cursor() as c:
					try:
						c.execute(statement, (bytes(16),))
					except pymysql.MySQLError as exc:
						denied.append((label, exc.args[0]))
					else:
						self.fail(f"{label} was NOT denied for {VIZ_RO_USER} — grant is too broad")
			# 1142 = ER_TABLEACCESS_DENIED: refused by privilege check, not by data shape.
			self.assertEqual([code for _, code in denied], [1142, 1142, 1142], denied)
		finally:
			conn.close()


@unittest.skipUnless(_SCHEMA_UP, f"microflows fixture DB not available at {DB_HOST}:{DB_PORT}")
class ServeTests(unittest.TestCase):
	"""The HTTP surface, running as viz_ro: static allowlist + /api/workflow."""

	@classmethod
	def setUpClass(cls) -> None:
		# Grant first (idempotent), so the server's DB user exists.
		conn = _root_connect()
		try:
			with conn.cursor() as c:
				for statement in _grant_statements():
					c.execute(statement)
		finally:
			conn.close()
		cls.db_cfg = dbq.DbConfig(host=DB_HOST, port=DB_PORT, user=VIZ_RO_USER,
		                          password=VIZ_RO_PWD, database=DB_NAME)
		cls.httpd = server.create_server("127.0.0.1", 0, cls.db_cfg, REPO_ROOT)
		cls.port = cls.httpd.server_address[1]
		cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
		cls.thread.start()

	@classmethod
	def tearDownClass(cls) -> None:
		cls.httpd.shutdown()
		cls.httpd.server_close()
		cls.thread.join(timeout=5)

	def _get(self, path: str):
		"""GET returning (status, headers, body-bytes); non-2xx does not raise."""
		try:
			with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
				return resp.status, dict(resp.headers), resp.read()
		except urllib.error.HTTPError as err:
			return err.code, dict(err.headers), err.read()

	def _get_json(self, path: str):
		status, _, body = self._get(path)
		return status, json.loads(body)

	# ===== static UI =====

	def test_root_serves_index_html(self) -> None:
		status, headers, body = self._get("/")
		self.assertEqual(status, 200)
		self.assertIn("text/html", headers["Content-Type"])
		self.assertEqual(body, (REPO_ROOT / "index.html").read_bytes())

	def test_vendor_and_plans_are_served(self) -> None:
		status, headers, body = self._get("/vendor/mermaid.min.js")
		self.assertEqual(status, 200)
		self.assertIn("javascript", headers["Content-Type"])
		self.assertGreater(len(body), 1_000_000)  # the vendored Mermaid is ~3.5 MB
		status, _, body = self._get("/plans/reserve_charge.mf")
		self.assertEqual(status, 200)
		self.assertEqual(body, (REPO_ROOT / "plans" / "reserve_charge.mf").read_bytes())

	def test_non_allowlisted_files_are_not_served(self) -> None:
		# These all exist on disk next to index.html — and must all be unreachable.
		for path in ("/pyproject.toml", "/justfile", "/src/mfviz/cli.py",
		             "/tests/test_serve.py", "/export_events.py"):
			status, payload = self._get_json(path)
			self.assertEqual(status, 404, path)
			self.assertEqual(payload["error"], "not_found", path)

	def test_path_traversal_is_rejected(self) -> None:
		# urllib normalizes "..", so drive the raw paths through http.client.
		for raw in ("/vendor/../pyproject.toml",
		            "/vendor/%2e%2e/pyproject.toml",
		            "/../pyproject.toml",
		            "/plans/../src/mfviz/cli.py"):
			conn = http.client.HTTPConnection("127.0.0.1", self.port)
			try:
				conn.putrequest("GET", raw, skip_accept_encoding=True)
				conn.endheaders()
				resp = conn.getresponse()
				body = resp.read()
				self.assertEqual(resp.status, 404, (raw, body[:200]))
			finally:
				conn.close()

	# ===== /api =====

	def test_health_reports_ok_through_viz_ro(self) -> None:
		status, payload = self._get_json("/api/health")
		self.assertEqual(status, 200, payload)
		self.assertTrue(payload["ok"])
		self.assertEqual(payload["database"], DB_NAME)

	def test_unknown_endpoint_is_404(self) -> None:
		status, payload = self._get_json("/api/nope")
		self.assertEqual(status, 404)
		self.assertEqual(payload["error"], "unknown_endpoint")

	def test_workflow_not_found_is_404(self) -> None:
		status, payload = self._get_json(f"/api/workflow/{MISSING_ID}")
		self.assertEqual(status, 404)
		self.assertEqual(payload["error"], "not_found")
		self.assertEqual(payload["workflow_id"], MISSING_ID)

	def test_workflow_bad_id_shape_is_404_unknown_endpoint(self) -> None:
		# The route itself requires exactly 32 hex chars; anything else never
		# reaches the DB layer.
		for bad in ("zz" * 16, "abc", "0" * 31, "0" * 33):
			status, payload = self._get_json(f"/api/workflow/{bad}")
			self.assertEqual(status, 404, bad)
			self.assertEqual(payload["error"], "unknown_endpoint", bad)

	def test_workflow_bad_max_depth_is_400(self) -> None:
		for bad in ("0", "-1", "abc"):
			status, payload = self._get_json(f"/api/workflow/{MISSING_ID}?max_depth={bad}")
			self.assertEqual(status, 400, bad)
			self.assertEqual(payload["error"], "bad_request", bad)

	def test_mutating_methods_are_rejected(self) -> None:
		conn = http.client.HTTPConnection("127.0.0.1", self.port)
		try:
			for method in ("POST", "PUT", "DELETE"):
				conn.request(method, f"/api/workflow/{MISSING_ID}")
				resp = conn.getresponse()
				body = resp.read()
				self.assertEqual(resp.status, 405, (method, body))
		finally:
			conn.close()

	def test_workflow_inspect_matches_query_layer(self) -> None:
		"""For a real workflow (when the fixture DB has one), the API response is
		exactly the query layer's tree — the HTTP layer adds nothing and drops
		nothing. Full mfinspect-parity harness lands with /api/workflows."""
		conn = _root_connect(database=DB_NAME)
		try:
			with conn.cursor() as c:
				c.execute("SELECT workflow_id FROM tb_mf_workflow ORDER BY created_at LIMIT 1")
				row = c.fetchone()
		finally:
			conn.close()
		if row is None:
			self.skipTest("fixture DB has no workflow rows to inspect")
		wf_hex = bytes(row[0]).hex()

		status, payload = self._get_json(f"/api/workflow/{wf_hex}?max_depth=3")
		self.assertEqual(status, 200, payload)

		ro_conn = dbq.connect(self.db_cfg)
		try:
			expected = dbq.inspect_workflow(ro_conn, wf_hex, 3)
		finally:
			ro_conn.close()
		self.assertEqual(payload, json.loads(json.dumps(expected, sort_keys=True)))
		self.assertEqual(payload["workflow_id"], wf_hex)
		for key in ("workflow", "plan", "args", "operations", "calls",
		            "checkpoints", "events", "children"):
			self.assertIn(key, payload)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
