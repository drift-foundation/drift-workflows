"""Focused static tests for the live-mode browser UI (work/viz-consolidation slice 3).

The repo's test pattern is Python unittest over the HTTP surface — there is no
browser engine in the dependency set (stdlib + PyMySQL only), so these tests pin
the CONTRACTS the live page must keep rather than rendered behavior:

  * served + allowlisted (and the demo player still served byte-exact);
  * self-contained: no external URLs in any src/href/fetch — the page's only
    network surface is same-origin /api/*;
  * no DB coupling: no DB host/port/credential strings in the UI code (the
    browser must never see them — the backend owns DB access);
  * no sequence-number ordering concept: chronology is event_ts, and the string
    `event_seq` must not appear.

The lint half (source inspection) runs without a DB; the serving half shares
test_serve's DB posture (skip locally, hard-required in gates).
"""
import re
import threading
import unittest
import urllib.request
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mfviz import dbq, server  # noqa: E402
import test_serve  # noqa: E402

LIVE = REPO_ROOT / "live.html"


class LiveUiLintTests(unittest.TestCase):
	"""Source-level contracts; no DB, no server."""

	@classmethod
	def setUpClass(cls) -> None:
		cls.text = LIVE.read_text(encoding="utf-8")

	def test_exists_and_is_allowlisted(self) -> None:
		self.assertTrue(LIVE.is_file())
		self.assertIn("live.html", server.STATIC_ALLOWLIST)

	def test_no_external_resources(self) -> None:
		# No absolute/external URL anywhere: no CDN scripts, fonts, images,
		# XHR targets. (mailto/data would also be suspicious in this page.)
		external = re.findall(r"(?:https?:)?//[\w.-]+", self.text)
		self.assertEqual(external, [], f"external references found: {external}")
		self.assertNotIn("<script src", self.text)
		self.assertNotIn("<link", self.text)

	def test_fetches_only_same_origin_api_paths(self) -> None:
		# Every fetch() goes through the single api() helper, and every path
		# handed to it starts with /api/. (fetch\((?!\)) skips the prose
		# mention "fetch()" in the page's header comment.)
		real_fetches = re.findall(r"fetch\((?!\))", self.text)
		self.assertEqual(len(real_fetches), 1, "all requests must go through api()")
		called = re.findall(r"api\(`([^`$]*)", self.text)
		self.assertTrue(called, "no api() call sites found")
		for path in called:
			self.assertTrue(path.startswith("/api/"), f"non-/api fetch target: {path!r}")

	def test_no_db_coupling(self) -> None:
		for needle in ("3306", "34214", "pymysql", "mariadb", "MDB_ROOT_PWD",
		               "VIZ_RO_PWD", "password", "db-host", "db_host"):
			self.assertNotIn(needle, self.text.lower().replace("-", "_")
			                 if needle == "db_host" else self.text,
			                 f"DB coupling leaked into the UI: {needle!r}")

	def test_no_event_seq_concept(self) -> None:
		self.assertNotIn("event_seq", self.text)
		# The timeline section names its actual ordering contract.
		self.assertIn("event_ts chronology", self.text)

	def test_no_browser_clock_or_date_arithmetic(self) -> None:
		# DB time is the authority: search bounds are copied verbatim from
		# /api/health (default_since/default_until, computed in SQL from NOW(6)).
		# Any Date construction would reintroduce browser timezone/DST
		# normalization into DB-time bounds.
		self.assertNotIn("new Date(", self.text)
		self.assertNotIn("Date.now(", self.text)
		self.assertIn("default_since", self.text)
		self.assertIn("default_until", self.text)
		# The until bound is inclusive of its whole second on DATETIME(6).
		self.assertIn('.999999', self.text)

	def test_full_inspect_link_present(self) -> None:
		self.assertIn("/api/workflow/${id}", self.text)


@unittest.skipUnless(test_serve._SCHEMA_UP,
                     f"microflows fixture DB not available at {test_serve.DB_HOST}:{test_serve.DB_PORT}")
class LiveUiServeTests(unittest.TestCase):
	"""The page (and the untouched demo player) through the real server."""

	@classmethod
	def setUpClass(cls) -> None:
		# Same posture as ServeTests: apply the grant (idempotent), then run the
		# server as the SELECT-only viz_ro user — root creds never reach the
		# server, so a future accidental API call from this suite cannot hide
		# behind elevated permissions.
		root = test_serve._root_connect()
		try:
			with root.cursor() as c:
				for statement in test_serve._grant_statements():
					c.execute(statement)
		finally:
			root.close()
		cls.db_cfg = dbq.DbConfig(
			host=test_serve.DB_HOST, port=test_serve.DB_PORT, user=test_serve.VIZ_RO_USER,
			password=test_serve.VIZ_RO_PWD, database=test_serve.DB_NAME)
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
		with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
			return resp.status, dict(resp.headers), resp.read()

	def test_live_html_is_served(self) -> None:
		status, headers, body = self._get("/live.html")
		self.assertEqual(status, 200)
		self.assertIn("text/html", headers["Content-Type"])
		self.assertEqual(body, LIVE.read_bytes())

	def test_demo_player_untouched_and_links_live_mode(self) -> None:
		status, _, body = self._get("/")
		self.assertEqual(status, 200)
		self.assertEqual(body, (REPO_ROOT / "index.html").read_bytes())
		self.assertIn(b'href="live.html"', body)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
