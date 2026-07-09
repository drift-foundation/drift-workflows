"""Fixture-backed tests for the slice-2 operator-question endpoints:
/api/workflow/<id>/tree, /timeline, /stuck (work/viz-consolidation).

One seeded workflow per stuck shape — every verdict the classifier can produce
is exercised through the HTTP surface, served as viz_ro:

  terminal, blocked_resolution (checkpoint resolution_required),
  running_under_lease, waiting_on_child (with descent past a terminal sibling
  to the deepest non-terminal descendant), redispatch_pending (outranking a
  simultaneously-set reconcile budget), reconcile_pending, scheduled_retry,
  claimable_now.

Plus the tree skeleton (fields, nesting, depth, truncation stubs) and the
timeline (merged cross-workflow event order by event_ts, depth annotations)
over a parent -> child -> grandchild fixture.

Reserved script namespace `mfviz-slice2`, fixed bee2… ids, FK-safe
self-cleanup. Same DB posture as test_serve.py (skip locally without the
fixture DB; MFVIZ_REQUIRE_DB=1 in gates makes absence a hard failure — the
check runs at import of test_serve, imported here).
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
import test_serve  # noqa: E402

SCRIPT = "mfviz-slice2"

def _wf(n: int) -> bytes:
	return bytes.fromhex(f"bee2{n:028x}")

W_TERM = _wf(0x01)      # terminal (completed)
W_BLOCKED = _wf(0x02)   # blocked_resolution + resolution_required checkpoint
W_LEASE = _wf(0x03)     # running_under_lease
W_RETRY = _wf(0x04)     # scheduled_retry
W_CLAIM = _wf(0x05)     # claimable_now
W_RECONC = _wf(0x06)    # reconcile_pending
W_REDISP = _wf(0x07)    # redispatch_pending (reconcile also set; escalation wins)
W_P = _wf(0x10)         # tree parent  (waiting_on_child)
W_TC = _wf(0x11)        # terminal child of W_P (must be skipped by descent)
W_C = _wf(0x12)         # non-terminal child of W_P (itself waiting on W_G)
W_G = _wf(0x13)         # grandchild (claimable_now -> deepest non-terminal)

ALL_IDS = (W_TERM, W_BLOCKED, W_LEASE, W_RETRY, W_CLAIM, W_RECONC, W_REDISP,
           W_P, W_TC, W_C, W_G)

T0 = "2026-01-01 12:00:00"
T1 = "2026-01-01 12:00:01"
T2 = "2026-01-01 12:00:02"
T3 = "2026-01-01 12:00:03"


def _cleanup(conn) -> None:
	marks = ", ".join(["%s"] * len(ALL_IDS))
	with conn.cursor() as c:
		for table in ("tb_mf_call", "tb_mf_operation", "tb_mf_workflow_checkpoint",
		              "tb_mf_workflow_event", "tb_mf_workflow_args", "tb_mf_workflow_plan"):
			c.execute(f"DELETE FROM {table} WHERE workflow_id IN ({marks})", ALL_IDS)
		# Children before parents (fk_mf_call_child is already gone with the calls).
		for wf_id in (W_G, W_C, W_TC, W_P, W_REDISP, W_RECONC, W_CLAIM, W_RETRY,
		              W_LEASE, W_BLOCKED, W_TERM):
			c.execute("DELETE FROM tb_mf_workflow WHERE workflow_id = %s", (wf_id,))


def _insert_workflow(c, wf_id: bytes, *, state: int, direction: int = 1,
                     disposition: int = 0, terminal_reason=None, return_json=None,
                     lease: bool = False, next_attempt_sql: str = "%s",
                     next_attempt_arg=T0, parent=None, node_id=None, root=None,
                     depth=None, created=T0) -> None:
	lease_owner_sql = "%s" if lease else "NULL"
	lease_exp_sql = "NOW(6) + INTERVAL 1 HOUR" if lease else "NULL"
	args: list = [wf_id, SCRIPT, state, direction, disposition, T2]
	if lease:
		args.append(b"\xee" * 16)
	if next_attempt_sql == "%s":
		args.append(next_attempt_arg)
	args += [terminal_reason, return_json, parent, node_id, root, depth, created, T2]
	c.execute(
		"INSERT INTO tb_mf_workflow (workflow_id, script_name, script_revision, state, "
		"execution_direction, current_disposition, current_event_ts, "
		"fencing_token, lease_owner, lease_expires_at, next_attempt_at, "
		"current_operation_attempt, continuation, terminal_reason, workflow_return_json, "
		"parent_workflow_id, parent_node_id, root_workflow_id, call_depth, created_at, "
		"updated_at) "
		f"VALUES (%s, %s, 1, %s, %s, %s, %s, 1, {lease_owner_sql}, {lease_exp_sql}, "
		f"{next_attempt_sql}, 0, '{{}}', %s, %s, %s, %s, %s, %s, %s, %s)",
		args)


def _seed(conn) -> None:
	future = "NOW(6) + INTERVAL 1 HOUR"
	with conn.cursor() as c:
		_insert_workflow(c, W_TERM, state=4, disposition=1, return_json="{}")
		_insert_workflow(c, W_BLOCKED, state=3, direction=2, disposition=2,
		                 terminal_reason="charge_rejected")
		c.execute(
			"INSERT INTO tb_mf_workflow_checkpoint (workflow_id, seq, operation_name, "
			"operation_id, payload, reversal_state, reverse_invocation_id, "
			"reverse_operation_name, reverse_schema_version, reverse_input_json, "
			"reverse_input_hash, created_at, updated_at) "
			"VALUES (%s, 1, 'reserve', %s, '{}', %s, %s, 'release', 1, '{}', %s, %s, %s)",
			(W_BLOCKED, b"\xb2" + b"\x00" * 15, dbq.CHECKPOINT_RESOLUTION_REQUIRED,
			 b"\xb3" + b"\x00" * 15, "0" * 64, T0, T1))
		_insert_workflow(c, W_LEASE, state=1, lease=True)
		_insert_workflow(c, W_RETRY, state=1, next_attempt_sql=future)
		_insert_workflow(c, W_CLAIM, state=1)
		# reconcile_pending: a pending op spending its route-404 budget.
		_insert_workflow(c, W_RECONC, state=1)
		c.execute(
			"INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, "
			"operation_name, schema_version, input_json, input_hash, call_kind, status, "
			"reconcile_attempts, reconcile_first_seen_at, reconcile_last_seen_at, "
			"reconcile_reason, created_at, updated_at) "
			"VALUES (%s, 1, %s, 'charge', 1, '{}', %s, 1, 1, 3, %s, %s, "
			"'participant_route_404', %s, %s)",
			(W_RECONC, b"\xc0" + b"\x00" * 15, "0" * 64, T0, T1, T0, T1))
		# redispatch_pending: escalation timer armed; reconcile columns ALSO set to
		# prove redispatch outranks it.
		_insert_workflow(c, W_REDISP, state=1)
		c.execute(
			"INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, "
			"operation_name, schema_version, input_json, input_hash, call_kind, status, "
			"reconcile_attempts, reconcile_first_seen_at, reconcile_last_seen_at, "
			"reconcile_reason, redispatch_first_seen_at, redispatch_last_at, "
			"redispatch_count, created_at, updated_at) "
			"VALUES (%s, 1, %s, 'charge', 1, '{}', %s, 1, 1, 1, %s, %s, "
			"'participant_route_404', %s, %s, 2, %s, %s)",
			(W_REDISP, b"\xd0" + b"\x00" * 15, "0" * 64, T0, T1, T0, T1, T0, T1))
		# Tree: P -> (seq 1) TC terminal, (seq 2) C -> (seq 1) G. P/C park with a
		# future next_attempt (waiting on the child, not due); G is due (deepest).
		_insert_workflow(c, W_P, state=1, next_attempt_sql=future, created=T0)
		_insert_workflow(c, W_TC, state=4, disposition=1, return_json="{}",
		                 parent=W_P, node_id="n1", root=W_P, depth=1, created=T1)
		_insert_workflow(c, W_C, state=1, next_attempt_sql=future,
		                 parent=W_P, node_id="n2", root=W_P, depth=1, created=T1)
		_insert_workflow(c, W_G, state=1,
		                 parent=W_C, node_id="n1", root=W_P, depth=2, created=T2)
		for wf_id, seq, op_id, child in ((W_P, 1, b"\xe1", W_TC), (W_P, 2, b"\xe2", W_C),
		                                 (W_C, 1, b"\xe3", W_G)):
			c.execute(
				"INSERT INTO tb_mf_operation (workflow_id, operation_seq, operation_id, "
				"operation_name, schema_version, input_json, input_hash, call_kind, status, "
				"result_json, created_at, updated_at) "
				"VALUES (%s, %s, %s, 'call:child', 1, '{}', %s, 2, %s, %s, %s, %s)",
				(wf_id, seq, op_id + b"\x00" * 15, "0" * 64,
				 2 if child is W_TC else 1, "{}" if child is W_TC else None, T0, T1))
			c.execute(
				"INSERT INTO tb_mf_call (workflow_id, operation_seq, child_workflow_id, "
				"child_script_name, child_plan_version, child_content_hash, child_status, "
				"first_requested_at, created_at, updated_at) "
				"VALUES (%s, %s, %s, %s, '1.0.0', %s, %s, %s, %s, %s)",
				(wf_id, seq, child, SCRIPT, b"\x02" * 33,
				 4 if child is W_TC else 1, T0, T0, T1))
		# Timeline events: interleaved event_ts across the three levels.
		for wf_id, ts, kind in ((W_P, T0, "created"),
		                        (W_C, T1, "created"),
		                        (W_G, T2, "created"),
		                        (W_P, T3, "operation_requested")):
			c.execute(
				"INSERT INTO tb_mf_workflow_event (workflow_id, event_ts, kind, "
				"payload) VALUES (%s, %s, %s, '{}')", (wf_id, ts, kind))


@unittest.skipUnless(test_serve._SCHEMA_UP,
                     f"microflows fixture DB not available at {test_serve.DB_HOST}:{test_serve.DB_PORT}")
class Slice2Tests(unittest.TestCase):
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

	def _stuck(self, wf_id: bytes):
		status, payload = self._api(f"/api/workflow/{wf_id.hex()}/stuck")
		self.assertEqual(status, 200, payload)
		return payload

	# ===== /tree =====

	def test_tree_shape_fields_and_depths(self) -> None:
		status, tree = self._api(f"/api/workflow/{W_P.hex()}/tree")
		self.assertEqual(status, 200, tree)
		self.assertEqual(tree["workflow_id"], W_P.hex())
		self.assertIsNone(tree["parent_workflow_id"])
		self.assertEqual(tree["depth"], 0)
		self.assertEqual(tree["script_name"], SCRIPT)
		self.assertEqual((tree["state"], tree["state_name"]), (1, "forward"))
		self.assertIn("current_disposition", tree)
		self.assertIn("terminal_reason", tree)
		self.assertNotIn("events", tree)      # skeletal: no heavy payload keys
		self.assertNotIn("operations", tree)
		kids = tree["children"]
		self.assertEqual([k["workflow_id"] for k in kids], [W_TC.hex(), W_C.hex()])
		self.assertEqual((kids[0]["state_name"], kids[0]["depth"]), ("completed", 1))
		self.assertEqual(kids[0]["parent_workflow_id"], W_P.hex())
		grand = kids[1]["children"]
		self.assertEqual([g["workflow_id"] for g in grand], [W_G.hex()])
		self.assertEqual(grand[0]["depth"], 2)
		self.assertEqual(grand[0]["children"], [])

	def test_tree_truncation_stub(self) -> None:
		status, tree = self._api(f"/api/workflow/{W_P.hex()}/tree?max_depth=1")
		self.assertEqual(status, 200, tree)
		grand = tree["children"][1]["children"]
		self.assertEqual(grand, [{"child_workflow_id": W_G.hex(), "truncated": True}])

	def test_tree_not_found_and_bad_depth(self) -> None:
		status, payload = self._api(f"/api/workflow/{'0' * 32}/tree")
		self.assertEqual(status, 404)
		self.assertEqual(payload["error"], "not_found")
		status, payload = self._api(f"/api/workflow/{W_P.hex()}/tree?max_depth=0")
		self.assertEqual(status, 400)

	# ===== /timeline =====

	def test_timeline_merged_order_and_annotations(self) -> None:
		status, tl = self._api(f"/api/workflow/{W_P.hex()}/timeline")
		self.assertEqual(status, 200, tl)
		events = tl["events"]
		self.assertEqual([(e["workflow_id"], e["kind"]) for e in events],
		                 [(W_P.hex(), "created"), (W_C.hex(), "created"),
		                  (W_G.hex(), "created"), (W_P.hex(), "operation_requested")])
		self.assertEqual([e["depth"] for e in events], [0, 1, 2, 0])
		for e in events:
			self.assertEqual(e["script_name"], SCRIPT)
			self.assertNotIn("event_seq", e)  # ordering is event_ts chronology, no seq exposed
			self.assertIn("event_ts", e)
			self.assertEqual(e["payload"], {})
		# The id->node map covers the whole tree (terminal child has no events but is listed).
		self.assertEqual(set(tl["workflows"]),
		                 {W_P.hex(), W_TC.hex(), W_C.hex(), W_G.hex()})

	def test_timeline_respects_max_depth(self) -> None:
		status, tl = self._api(f"/api/workflow/{W_P.hex()}/timeline?max_depth=1")
		self.assertEqual(status, 200, tl)
		# Grandchild is beyond the depth budget: excluded from ids and events.
		self.assertNotIn(W_G.hex(), tl["workflows"])
		self.assertEqual([e["workflow_id"] for e in tl["events"]],
		                 [W_P.hex(), W_C.hex(), W_P.hex()])

	# ===== /stuck: one test per verdict =====

	def test_stuck_terminal(self) -> None:
		v = self._stuck(W_TERM)
		self.assertEqual(v["verdict"], "terminal")
		self.assertEqual(v["evidence"]["state_name"], "completed")

	def test_stuck_blocked_resolution(self) -> None:
		v = self._stuck(W_BLOCKED)
		self.assertEqual(v["verdict"], "blocked_resolution")
		self.assertEqual(v["evidence"]["terminal_reason"], "charge_rejected")
		self.assertEqual(len(v["resolution_required"]), 1)
		ckpt = v["resolution_required"][0]
		self.assertEqual(ckpt["reversal_state"], dbq.CHECKPOINT_RESOLUTION_REQUIRED)
		self.assertEqual(ckpt["operation_name"], "reserve")
		self.assertEqual(ckpt["reverse_operation_name"], "release")

	def test_stuck_running_under_lease(self) -> None:
		v = self._stuck(W_LEASE)
		self.assertEqual(v["verdict"], "running_under_lease")
		self.assertEqual(v["evidence"]["lease_owner"], "ee" * 16)
		self.assertGreater(v["evidence"]["lease_expires_at"], v["db_now"])

	def test_stuck_scheduled_retry(self) -> None:
		v = self._stuck(W_RETRY)
		self.assertEqual(v["verdict"], "scheduled_retry")
		self.assertGreater(v["evidence"]["next_attempt_at"], v["db_now"])

	def test_stuck_claimable_now(self) -> None:
		v = self._stuck(W_CLAIM)
		self.assertEqual(v["verdict"], "claimable_now")
		self.assertIsNone(v["evidence"]["lease_owner"])
		self.assertLess(v["evidence"]["next_attempt_at"], v["db_now"])

	def test_stuck_reconcile_pending(self) -> None:
		v = self._stuck(W_RECONC)
		self.assertEqual(v["verdict"], "reconcile_pending")
		self.assertEqual(len(v["reconcile"]), 1)
		row = v["reconcile"][0]
		self.assertEqual(row["reconcile_attempts"], 3)
		self.assertEqual(row["reconcile_reason"], "participant_route_404")
		self.assertIsNotNone(row["reconcile_first_seen_at"])
		self.assertIsNotNone(row["reconcile_last_seen_at"])

	def test_stuck_redispatch_outranks_reconcile(self) -> None:
		v = self._stuck(W_REDISP)
		self.assertEqual(v["verdict"], "redispatch_pending")
		self.assertEqual(len(v["redispatch"]), 1)
		row = v["redispatch"][0]
		self.assertEqual(row["redispatch_count"], 2)
		self.assertIsNotNone(row["redispatch_first_seen_at"])
		self.assertIsNotNone(row["redispatch_last_at"])
		# The reconcile budget is also visibly set on the same row — the verdict
		# chose the escalation, it did not hide the underlying budget.
		self.assertEqual(row["reconcile_attempts"], 1)

	def test_stuck_waiting_on_child_descends_past_terminal_sibling(self) -> None:
		v = self._stuck(W_P)
		self.assertEqual(v["verdict"], "waiting_on_child")
		# Terminal sibling (seq 1) is skipped; descent goes P -> C -> G.
		self.assertEqual(v["path"], [W_P.hex(), W_C.hex(), W_G.hex()])
		child = v["waiting_on"]
		self.assertEqual(child["workflow_id"], W_C.hex())
		self.assertEqual(child["verdict"], "waiting_on_child")
		deepest = child["waiting_on"]
		self.assertEqual(deepest["workflow_id"], W_G.hex())
		self.assertEqual(deepest["verdict"], "claimable_now")

	def test_stuck_not_found(self) -> None:
		status, payload = self._api(f"/api/workflow/{'0' * 32}/stuck")
		self.assertEqual(status, 404)
		self.assertEqual(payload["error"], "not_found")


if __name__ == "__main__":  # pragma: no cover
	unittest.main()
