"""Fixture-owned API golden tests (work/viz-consolidation slice 4).

The successor to the temporary mfinspect parity harness: the API's inspect and
list/search responses are asserted byte-for-byte (as parsed JSON) against
COMMITTED golden files in tests/goldens/, over the same deterministic seeded
fixture tree the parity harness used (parent -> settled call -> completed
child, plan/args/operation/checkpoint/events on both, fixed ids/timestamps,
reserved script name, FK-safe self-cleanup).

PROVENANCE: the original two-node goldens were minted from the API on
2026-07-10 while the parity harness was still green, each asserted equal to the
mfinspect zipapp's own output at mint time — so the JSON contract they pin is
"mfinspect as of its retirement". The fixture was then extended with a
GRANDCHILD (same ported truncation semantics, stub shape unchanged) so the
truncated-inspect golden actually exercises truncation: at max_depth=1 the
grandchild must come back as a {"child_workflow_id": ..., "truncated": true}
stub — review-caught: with only parent->child, max_depth=1 truncates nothing
and the "truncated" golden was byte-identical to the full one. If the API's
JSON contract changes deliberately, re-mint the goldens and review the diff
like any contract change.

Also carries the error-surface coverage that lived in the parity harness:
the bounded-scan 400 combinations and the unknown-state 400, plus the
not-found / bad-max-depth bounds for the inspect route.

Same DB posture as test_serve.py (skip locally without the fixture DB;
MFVIZ_REQUIRE_DB=1 in gates makes absence a hard error — enforced at import of
test_serve, imported here). Served as viz_ro throughout.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mfviz import dbq, server  # noqa: E402
import test_serve  # noqa: E402  (shared DB helpers + the MFVIZ_REQUIRE_DB import-time check)

GOLDENS = REPO_ROOT / "tests" / "goldens"

# Reserved golden-fixture namespace: a script name no real fixture/integration
# data uses, and fixed ids/timestamps, so responses are fully deterministic.
SCRIPT = "mfviz-parity"
PARENT_ID = bytes.fromhex("beef0000000000000000000000000001")
CHILD_ID = bytes.fromhex("beef0000000000000000000000000002")
GRANDCHILD_ID = bytes.fromhex("beef0000000000000000000000000003")
OP_ID = bytes.fromhex("beef0000000000000000000000000101")
CKPT_OP_ID = bytes.fromhex("beef0000000000000000000000000102")
CHILD_OP_ID = bytes.fromhex("beef0000000000000000000000000103")
SINCE = "2026-01-01 00:00:00"
UNTIL = "2026-01-02 00:00:00"
T0 = "2026-01-01 12:00:00"
T1 = "2026-01-01 12:00:01"
T2 = "2026-01-01 12:00:02"


def _cleanup(conn) -> None:
	"""Remove every fixture row (FK-safe order). Idempotent."""
	ids = (PARENT_ID, CHILD_ID, GRANDCHILD_ID)
	with conn.cursor() as c:
		for table in ("tb_mf_call", "tb_mf_operation", "tb_mf_workflow_checkpoint",
		              "tb_mf_workflow_event", "tb_mf_workflow_args", "tb_mf_workflow_plan"):
			c.execute(f"DELETE FROM {table} WHERE workflow_id IN (%s, %s, %s)", ids)
		for wf_id in (GRANDCHILD_ID, CHILD_ID, PARENT_ID):  # leaves before parents
			c.execute("DELETE FROM tb_mf_workflow WHERE workflow_id = %s", (wf_id,))


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
		# Grandchild: completed, depth 2 under the child — exists so max_depth=1
		# genuinely truncates (depth+1 > max_depth stubs the GRANDchild, not the child).
		c.execute(
			"INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
			"execution_direction, current_disposition, current_event_ts, "
			"fencing_token, next_attempt_at, current_operation_attempt, continuation, "
			"workflow_return_json, parent_workflow_id, parent_node_id, root_workflow_id, "
			"call_depth, created_at, updated_at) "
			"VALUES (%s, %s, 1, 4, 1, 1, %s, 1, %s, 0, '{}', '{}', %s, 'n1', %s, 2, %s, %s)",
			(GRANDCHILD_ID, SCRIPT, T2, T1, CHILD_ID, PARENT_ID, T1, T2))
		for wf_id, hash_byte in ((PARENT_ID, b"\x01"), (CHILD_ID, b"\x02"),
		                         (GRANDCHILD_ID, b"\x03")):
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
		# Child's own settled call (seq 1) -> the grandchild, with its sidecar row.
		c.execute(
			"INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, "
			"operation_name, schema_version, input_json, input_hash, call_kind, status, "
			"result_json, created_at, updated_at) "
			"VALUES (%s, 1, %s, 'call:n1', 1, '{}', %s, 2, 2, '{}', %s, %s)",
			(CHILD_ID, CHILD_OP_ID, "0" * 64, T0, T1))
		c.execute(
			"INSERT INTO tb_mf_call (workflow_id, operation_seq, child_workflow_id, "
			"child_script_name, child_plan_version, child_content_hash, child_status, "
			"first_requested_at, created_at, updated_at) "
			"VALUES (%s, 1, %s, %s, '1.0.0', %s, 4, %s, %s, %s)",
			(CHILD_ID, GRANDCHILD_ID, SCRIPT, b"\x03" * 33, T0, T0, T1))
		# A forward checkpoint on the parent (reversal_state 1 = forward-held).
		c.execute(
			"INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, "
			"operation_id, payload, reversal_state, created_at, updated_at) "
			"VALUES (%s, 1, 'reserve', %s, '{}', 1, %s, %s)",
			(PARENT_ID, CKPT_OP_ID, T0, T0))
		# Event history: two per node, strictly increasing event_ts per workflow.
		for wf_id, kinds in ((PARENT_ID, ("created", "operation_requested")),
		                     (CHILD_ID, ("created", "completed")),
		                     (GRANDCHILD_ID, ("created", "completed"))):
			for i, kind in enumerate(kinds):
				c.execute(
					"INSERT INTO tb_mf_workflow_event (workflow_id, event_ts, kind, "
					"payload) VALUES (%s, %s, %s, '{}')",
					(wf_id, T2 if i == 1 else T0, kind))


def _golden(name: str):
	return json.loads((GOLDENS / name).read_text(encoding="utf-8"))


@unittest.skipUnless(test_serve._SCHEMA_UP,
                     f"microflows fixture DB not available at {test_serve.DB_HOST}:{test_serve.DB_PORT}")
class GoldenTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls) -> None:
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

	# ===== inspect goldens =====

	def test_inspect_full_matches_golden(self) -> None:
		status, api = self._api(f"/api/workflow/{PARENT_ID.hex()}?max_depth=5")
		self.assertEqual(status, 200, api)
		self.assertEqual(api, _golden("inspect_full.json"))
		# Structural guard (independent of the golden): the grandchild is fully
		# expanded at this depth.
		grand = api["children"][0]["children"][0]
		self.assertEqual(grand["workflow_id"], GRANDCHILD_ID.hex())
		self.assertIn("events", grand)

	def test_inspect_truncated_matches_golden(self) -> None:
		status, api = self._api(f"/api/workflow/{PARENT_ID.hex()}?max_depth=1")
		self.assertEqual(status, 200, api)
		self.assertEqual(api, _golden("inspect_truncated.json"))
		# Structural guard: max_depth=1 expands the depth-1 child but STUBS the
		# grandchild — this golden must genuinely differ from the full one
		# (review-caught: a 2-node fixture truncated nothing and the two goldens
		# were byte-identical).
		self.assertEqual(api["children"][0]["children"],
		                 [{"child_workflow_id": GRANDCHILD_ID.hex(), "truncated": True}])
		self.assertNotEqual(api, _golden("inspect_full.json"))

	# ===== list goldens =====

	_Q = f"script={SCRIPT}&since={SINCE.replace(' ', '+')}&until={UNTIL.replace(' ', '+')}"

	def test_list_unfiltered_matches_golden(self) -> None:
		status, api = self._api(f"/api/workflows?{self._Q}")
		self.assertEqual(status, 200, api)
		self.assertEqual(api, _golden("list_unfiltered.json"))

	def test_list_state_filter_matches_golden(self) -> None:
		status, api = self._api(f"/api/workflows?{self._Q}&state=completed")
		self.assertEqual(status, 200, api)
		self.assertEqual(api, _golden("list_state_completed.json"))

	def test_list_plan_version_filter_matches_golden(self) -> None:
		status, api = self._api(f"/api/workflows?{self._Q}&plan_version=1.0.0")
		self.assertEqual(status, 200, api)
		self.assertEqual(api, _golden("list_plan_version.json"))

	# ===== error surface (moved here from the retired parity harness) =====

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
		status, payload = self._api(f"/api/workflows?{self._Q}&state=nonsense")
		self.assertEqual(status, 400)
		self.assertEqual(payload["error"], "bad_request")
		self.assertIn("unknown state name", payload["detail"])

	def test_golden_timestamps_uniform_utc(self) -> None:
		"""F1 contract over the committed goldens: every timestamp renders at
		fixed microsecond precision with a trailing Z — mixed shapes would break
		lexicographic chronology within a second."""
		import re
		ts_any = re.compile(r'"(\d{4}-\d{2}-\d{2}T[0-9:.]+Z?)"')
		full_shape = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
		for name in ("inspect_full.json", "inspect_truncated.json", "list_unfiltered.json",
		             "list_state_completed.json", "list_plan_version.json"):
			text = (GOLDENS / name).read_text(encoding="utf-8")
			stamps = ts_any.findall(text)
			self.assertTrue(stamps, name)
			bad = [s for s in stamps if not full_shape.match(s)]
			self.assertEqual(bad, [], (name, bad[:3]))

	def test_inspect_not_found_and_bad_depth(self) -> None:
		status, payload = self._api(f"/api/workflow/{'0' * 32}")
		self.assertEqual(status, 404)
		self.assertEqual(payload["error"], "not_found")
		status, payload = self._api(f"/api/workflow/{PARENT_ID.hex()}?max_depth=0")
		self.assertEqual(status, 400)
		self.assertEqual(payload["error"], "bad_request")


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
