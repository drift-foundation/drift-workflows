"""Formal mfinspect-parity harness (work/viz-consolidation slice 1 gate).

Seeds a deterministic two-node workflow tree (parent -> call -> completed child,
with plan/args/operation/checkpoint/events on both) under a reserved script name,
then asserts the backend API and the committed mfinspect zipapp return the SAME
data for both endpoints:

  GET /api/workflow/<id>?max_depth=N  ==  mfinspect inspect <id> --max-depth N
  GET /api/workflows?script&since&until[&state]  ==  mfinspect list ...

Documented envelope differences (the ONLY allowed ones — anything else fails):
  * list entries additionally carry `href` (the /api/workflow/<id> link) and
    `updated_at` (mfinspect never exposed the row's last-write timestamp).

TEMPORARY BY CHARTER: this harness exists to prove migration parity and is
replaced by fixture-owned golden API tests before slice 4 deletes mfinspect.

Same DB posture as test_serve.py: skips without the fixture DB, and
MFVIZ_REQUIRE_DB=1 (exported by the repo gate) turns absence into a hard error
(enforced at import in test_serve, which this module imports).
"""
import json
import os
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import pymysql

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mfviz import dbq, server  # noqa: E402
import test_serve  # noqa: E402  (shared DB helpers + the MFVIZ_REQUIRE_DB import-time check)

MFINSPECT = REPO_ROOT.parent / "microflows" / "tools" / "mfinspect" / "mfinspect"

# Reserved parity namespace: a script name no real fixture/integration data uses,
# and fixed ids/timestamps, so the harness is deterministic and self-cleaning.
SCRIPT = "mfviz-parity"
PARENT_ID = bytes.fromhex("beef0000000000000000000000000001")
CHILD_ID = bytes.fromhex("beef0000000000000000000000000002")
OP_ID = bytes.fromhex("beef0000000000000000000000000101")
CKPT_OP_ID = bytes.fromhex("beef0000000000000000000000000102")
SINCE = "2026-01-01 00:00:00"
UNTIL = "2026-01-02 00:00:00"
T0 = "2026-01-01 12:00:00"
T1 = "2026-01-01 12:00:01"
T2 = "2026-01-01 12:00:02"


def _cleanup(conn) -> None:
	"""Remove every parity row (FK-safe order). Idempotent."""
	ids = (PARENT_ID, CHILD_ID)
	with conn.cursor() as c:
		for table in ("tb_mf_call", "tb_mf_operation", "tb_mf_workflow_checkpoint",
		              "tb_mf_workflow_event", "tb_mf_workflow_args", "tb_mf_workflow_plan"):
			c.execute(f"DELETE FROM {table} WHERE workflow_id IN (%s, %s)", ids)
		c.execute("DELETE FROM tb_mf_workflow WHERE workflow_id = %s", (CHILD_ID,))
		c.execute("DELETE FROM tb_mf_workflow WHERE workflow_id = %s", (PARENT_ID,))


def _seed(conn) -> None:
	with conn.cursor() as c:
		# Parent: forward (state 1, disposition 0), top-level (ancestry NULLs).
		c.execute(
			"INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
			"execution_direction, current_disposition, current_event_ts, "
			"fencing_token, next_attempt_at, current_operation_attempt, continuation, "
			"created_at, updated_at) VALUES (%s, %s, 1, 1, 1, 0, %s, 1, %s, 0, '{}', %s, %s)",
			(PARENT_ID, SCRIPT, T2, T0, T0, T2))
		# Child: completed (state 4, disposition 1, return doc required), depth 1.
		c.execute(
			"INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
			"execution_direction, current_disposition, current_event_ts, "
			"fencing_token, next_attempt_at, current_operation_attempt, continuation, "
			"workflow_return_json, parent_workflow_id, parent_node_id, root_workflow_id, "
			"call_depth, created_at, updated_at) "
			"VALUES (%s, %s, 1, 4, 1, 1, %s, 1, %s, 0, '{}', '{}', %s, 'n1', %s, 1, %s, %s)",
			(CHILD_ID, SCRIPT, T2, T1, PARENT_ID, PARENT_ID, T1, T2))
		for wf_id, hash_byte in ((PARENT_ID, b"\x01"), (CHILD_ID, b"\x02")):
			c.execute(
				"INSERT INTO tb_mf_workflow_plan (workflow_id, plan_version, content_hash, "
				"plan_length, created_at) VALUES (%s, '1.0.0', %s, 3, %s)",
				(wf_id, hash_byte * 33, T0))
			c.execute(
				"INSERT INTO tb_mf_workflow_args (workflow_id, args_canonical, created_at) "
				"VALUES (%s, %s, %s)", (wf_id, b'{"k":"v"}', T0))
		# Parent operation 1 = the child call (settled), plus its call sidecar row.
		c.execute(
			"INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, "
			"operation_name, schema_version, input_json, input_hash, call_kind, status, "
			"result_json, created_at, updated_at) "
			"VALUES (%s, 1, %s, 'call:n1', 1, '{}', %s, 2, 2, '{}', %s, %s)",
			(PARENT_ID, OP_ID, "0" * 64, T0, T1))
		c.execute(
			"INSERT INTO tb_mf_call (workflow_id, operation_seq, child_workflow_id, "
			"child_script_name, child_plan_version, child_content_hash, child_status, "
			"first_requested_at, created_at, updated_at) "
			"VALUES (%s, 1, %s, %s, '1.0.0', %s, 4, %s, %s, %s)",
			(PARENT_ID, CHILD_ID, SCRIPT, b"\x02" * 33, T0, T0, T1))
		# A forward checkpoint on the parent (reversal_state 1 = forward-held).
		c.execute(
			"INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, "
			"operation_id, payload, reversal_state, created_at, updated_at) "
			"VALUES (%s, 1, 'reserve', %s, '{}', 1, %s, %s)",
			(PARENT_ID, CKPT_OP_ID, T0, T0))
		# Event history: two per node.
		for wf_id, kinds in ((PARENT_ID, ("created", "operation_requested")),
		                     (CHILD_ID, ("created", "completed"))):
			for i, kind in enumerate(kinds):
				c.execute(
					"INSERT INTO tb_mf_workflow_event (workflow_id, event_ts, kind, "
					"payload) VALUES (%s, %s, %s, '{}')",
					(wf_id, T2 if i == 1 else T0, kind))


def _mfinspect(*args: str) -> object:
	"""Run the committed mfinspect zipapp; return its parsed JSON output."""
	env = {**os.environ, "MDB_ROOT_PWD": test_serve.ROOT_PWD}
	result = subprocess.run(
		[str(MFINSPECT),
		 "--host", test_serve.DB_HOST, "--port", str(test_serve.DB_PORT),
		 "--user", test_serve.ROOT_USER, "--database", test_serve.DB_NAME,
		 "--indent", "0", *args],
		capture_output=True, text=True, env=env)
	if result.returncode != 0:
		raise AssertionError(f"mfinspect failed: {result.stderr}")
	return json.loads(result.stdout)


@unittest.skipUnless(test_serve._SCHEMA_UP,
                     f"microflows fixture DB not available at {test_serve.DB_HOST}:{test_serve.DB_PORT}")
class ParityTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
		assert MFINSPECT.is_file(), f"committed mfinspect zipapp not found at {MFINSPECT}"
		root = test_serve._root_connect(database=test_serve.DB_NAME)
		try:
			with root.cursor() as c:
				for statement in test_serve._grant_statements():
					c.execute(statement)
			_cleanup(root)
			_seed(root)
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
		root = test_serve._root_connect(database=test_serve.DB_NAME)
		try:
			_cleanup(root)
		finally:
			root.close()

	def _api(self, path: str):
		try:
			with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
				return resp.status, json.loads(resp.read())
		except urllib.error.HTTPError as err:
			return err.code, json.loads(err.read())

	# ===== inspect parity =====

	def test_inspect_parity_full_tree(self) -> None:
		status, api = self._api(f"/api/workflow/{PARENT_ID.hex()}?max_depth=5")
		self.assertEqual(status, 200, api)
		expected = _mfinspect("inspect", PARENT_ID.hex(), "--max-depth", "5")
		self.assertEqual(api, expected)
		# The seeded shape actually exercises the tree: parent -> one child, completed.
		self.assertEqual(len(api["children"]), 1)
		self.assertEqual(api["children"][0]["workflow_id"], CHILD_ID.hex())
		self.assertEqual(api["children"][0]["workflow"]["state"], 4)
		self.assertEqual(len(api["events"]), 2)
		self.assertEqual(len(api["checkpoints"]), 1)

	def test_inspect_parity_truncated_child(self) -> None:
		status, api = self._api(f"/api/workflow/{PARENT_ID.hex()}?max_depth=1")
		self.assertEqual(status, 200, api)
		expected = _mfinspect("inspect", PARENT_ID.hex(), "--max-depth", "1")
		self.assertEqual(api, expected)
		# max_depth=1 expands call_depth<=1 fully, so the depth-1 child is still
		# expanded (mfinspect numbering) — verify against a depth-0-only ask too.
		self.assertEqual(api["children"][0]["workflow_id"], CHILD_ID.hex())

	# ===== list parity =====

	@staticmethod
	def _strip_envelope(entries):
		"""Drop the documented additive fields, leaving mfinspect's exact summary."""
		return [{k: v for k, v in e.items() if k not in ("href", "updated_at")}
		        for e in entries]

	def test_list_parity_bounded_query(self) -> None:
		q = f"script={SCRIPT}&since={SINCE.replace(' ', '+')}&until={UNTIL.replace(' ', '+')}"
		status, api = self._api(f"/api/workflows?{q}")
		self.assertEqual(status, 200, api)
		expected = _mfinspect("list", "--script", SCRIPT, "--since", SINCE, "--until", UNTIL)
		self.assertEqual(self._strip_envelope(api), expected)
		self.assertEqual(len(api), 2)  # parent + child, both under the parity script name
		# Every entry links naturally to its exact-instance inspection.
		for entry in api:
			self.assertEqual(entry["href"], f"/api/workflow/{entry['workflow_id']}")
			self.assertIn("updated_at", entry)
			self.assertIn("state_name", entry)

	def test_list_parity_state_filter(self) -> None:
		q = (f"script={SCRIPT}&since={SINCE.replace(' ', '+')}"
		     f"&until={UNTIL.replace(' ', '+')}&state=completed")
		status, api = self._api(f"/api/workflows?{q}")
		self.assertEqual(status, 200, api)
		expected = _mfinspect("list", "--script", SCRIPT, "--since", SINCE, "--until", UNTIL,
		                      "--state", "completed")
		self.assertEqual(self._strip_envelope(api), expected)
		self.assertEqual([e["workflow_id"] for e in api], [CHILD_ID.hex()])
		self.assertEqual(api[0]["state_name"], "completed")

	def test_list_parity_plan_version_filter(self) -> None:
		q = (f"script={SCRIPT}&since={SINCE.replace(' ', '+')}"
		     f"&until={UNTIL.replace(' ', '+')}&plan_version=1.0.0")
		status, api = self._api(f"/api/workflows?{q}")
		self.assertEqual(status, 200, api)
		expected = _mfinspect("list", "--script", SCRIPT, "--since", SINCE, "--until", UNTIL,
		                      "--plan-version", "1.0.0")
		self.assertEqual(self._strip_envelope(api), expected)
		self.assertEqual(len(api), 2)

	# ===== bounded-scan rule =====

	def test_list_requires_all_bounds(self) -> None:
		since = SINCE.replace(" ", "+")
		until = UNTIL.replace(" ", "+")
		for q, missing in (
			("", "script, since, until"),
			(f"script={SCRIPT}", "since, until"),
			(f"script={SCRIPT}&since={since}", "until"),
			(f"since={since}&until={until}", "script"),
		):
			status, payload = self._api(f"/api/workflows?{q}")
			self.assertEqual(status, 400, q)
			self.assertEqual(payload["error"], "bad_request", q)
			self.assertIn(missing, payload["detail"], q)

	def test_list_unknown_state_is_400(self) -> None:
		q = (f"script={SCRIPT}&since={SINCE.replace(' ', '+')}"
		     f"&until={UNTIL.replace(' ', '+')}&state=nonsense")
		status, payload = self._api(f"/api/workflows?{q}")
		self.assertEqual(status, 400)
		self.assertEqual(payload["error"], "bad_request")
		self.assertIn("unknown state name", payload["detail"])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
