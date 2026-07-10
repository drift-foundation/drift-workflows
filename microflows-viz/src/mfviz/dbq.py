"""Read-only coordinator-DB query layer for the microflows-viz backend.

The connection, decode, and fetch helpers plus the recursive
`inspect_workflow` tree walk originated in the retired mfinspect CLI
(work/viz-consolidation slices 1–4); microflows-viz is its successor and the
single operator tool. The JSON contracts are pinned by fixture-owned golden
tests (tests/test_golden.py) minted at mfinspect's retirement.

READ-ONLY BY DESIGN, ENFORCED BY PERMISSIONS: every query below is a SELECT, and
the backend is meant to run as the SELECT-only `viz_ro` DB user
(microflows/db/grants/viz_ro.sql) so a mutating statement fails at the DB rather
than surviving as an unnoticed code path.

`inspect_workflow` output shape (one JSON-safe dict per workflow node; `children`
nests recursively):
  {
    "workflow_id": "<32-hex>",
    "workflow": {...columns from tb_mf_workflow, decoded...},
    "plan": {...tb_mf_workflow_plan row, or null (legacy single-op workflow)...},
    "args": {...decoded tb_mf_workflow_args.args_canonical, or null...},
    "operations": [...tb_mf_operation rows ordered by operation_seq...],
    "calls": [...tb_mf_call rows ordered by operation_seq...],
    "checkpoints": [...tb_mf_workflow_checkpoint rows ordered by seq...],
      NB (consumer-hardening F2): operations[] and checkpoints[] both carry
      reconcile_*/redispatch_* counters and they are NOT duplicates —
      operations[] counters are authoritative for FORWARD dispatch of that
      operation; the same-named checkpoints[] counters track the checkpoint's
      own COMPENSATION (reverse) dispatch and are legitimately 0/null on any
      workflow that never reversed. Read forward retry/reclaim/redispatch
      state from operations[], never from checkpoints[].
    "events": [...tb_mf_workflow_event rows ordered by event_ts, full history...],
    "children": [...recursive child nodes, or {"child_workflow_id":...,
                  "truncated": true} stubs once max_depth is reached -- never
                  silently omitted...]
  }
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

try:
	import pymysql  # type: ignore
	from pymysql.cursors import DictCursor  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - captured at runtime
	pymysql = None
	DictCursor = None


DEFAULT_MAX_DEPTH = 10

# state codes (tb_mf_workflow.sql / packages/microflows/src/state.drift).
STATE_NAMES = {
	1: "forward",
	2: "reversing",
	3: "blocked_resolution",
	4: "completed",
	5: "reversed",
	6: "resolved_exception",
	7: "failed",
}
STATE_CODES = {name: code for code, name in STATE_NAMES.items()}


class MfvizError(Exception):
	"""Base exception for user-facing errors."""


class DbNotUtcError(Exception):
	"""The coordinator DB clock is not UTC, so the API's Z-designated timestamps
	would be FALSE. Raised by connect() before any timestamped payload can be
	built — the backend fails closed (502) instead of emitting wrong data."""


def resolve_state(value: str | None) -> int | None:
	"""A state filter, by numeric code or name; None passes through (no filter)."""
	if value is None:
		return None
	if value.isdigit():
		code = int(value)
		if code not in STATE_NAMES:
			raise MfvizError(f"unknown state code {value!r}; valid codes: "
			                  f"{sorted(STATE_NAMES)}")
		return code
	name = value.lower()
	if name not in STATE_CODES:
		raise MfvizError(f"unknown state name {value!r}; valid names: "
		                  f"{sorted(STATE_CODES)}")
	return STATE_CODES[name]


@dataclass(frozen=True)
class DbConfig:
	host: str
	port: int
	user: str
	password: str
	database: str


def connect(cfg: DbConfig):
	if pymysql is None:  # pragma: no cover - captured at runtime
		raise MfvizError("PyMySQL is not available in this interpreter")
	conn = pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
	                       password=cfg.password, database=cfg.database,
	                       autocommit=True, cursorclass=DictCursor)
	_assert_db_utc(conn)
	return conn


def _assert_db_utc(conn) -> None:
	"""ENFORCE the timestamp contract, not just report it: every connection is
	checked before use, so a non-UTC DB/session can never produce a Z-designated
	payload. /api/health's db_utc_offset_seconds is the observable; this is the
	guard (fail closed with a config error, per consumer-hardening F1 review)."""
	with conn.cursor() as c:
		c.execute("SELECT TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(6), NOW(6)) AS off")
		off = int(c.fetchone()["off"])
	if off != 0:
		conn.close()
		raise DbNotUtcError(
			f"coordinator DB clock is not UTC (NOW() is {off:+d}s from UTC_TIMESTAMP()); "
			"refusing to serve Z-designated timestamps — fix the DB/session time zone "
			"(the deployment contract requires the coordinator database to run UTC)")


def _hex_bytes(value_hex: str, field: str) -> bytes:
	try:
		b = bytes.fromhex(value_hex)
	except ValueError as exc:
		raise MfvizError(f"invalid {field} (not hex): {value_hex!r}") from exc
	if len(b) != 16:
		raise MfvizError(
			f"invalid {field} (expected 16 bytes / 32 hex chars, got {len(b)} bytes): {value_hex!r}"
		)
	return b


def _decode_value(v):
	"""Render a raw DB value JSON-safely: bytes -> lowercase hex, datetime ->
	ISO 8601 **UTC with a trailing Z at fixed microsecond precision**
	(consumer-hardening F1). Fixed precision matters: mixed shapes break
	lexicographic chronology within a second ("…00Z" would sort AFTER
	"…00.123456Z" despite being earlier), so every timestamp renders exactly
	six fractional digits. The Z is truthful because connect() fails closed on
	a non-UTC DB (_assert_db_utc); /api/health reports the offset observable."""
	if v is None:
		return None
	if isinstance(v, (bytes, bytearray)):
		return bytes(v).hex()
	if isinstance(v, datetime):
		return v.isoformat(timespec="microseconds") + "Z"
	return v


def _decode_row(row):
	if row is None:
		return None
	return {k: _decode_value(v) for k, v in row.items()}


def _decode_json_field(row, key: str):
	"""Parse a JSON-document column (stored as TEXT) into a real JSON value; None stays None."""
	if row is None:
		return None
	raw = row.get(key)
	if raw is None:
		return None
	return json.loads(raw)


# ===== exact-instance inspection (golden-pinned JSON contract) =====

def fetch_workflow(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_workflow WHERE workflow_id = %s", (wf_bytes,))
		row = c.fetchone()
	if row is None:
		return None
	out = _decode_row(row)
	out["continuation"] = _decode_json_field(row, "continuation")
	out["workflow_return_json"] = _decode_json_field(row, "workflow_return_json")
	return out


def fetch_plan(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_workflow_plan WHERE workflow_id = %s", (wf_bytes,))
		row = c.fetchone()
	return _decode_row(row)


def fetch_args(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_workflow_args WHERE workflow_id = %s", (wf_bytes,))
		row = c.fetchone()
	if row is None:
		return None
	out = _decode_row(row)
	# args_canonical is UTF-8 bytes holding a JSON object, not a JSON-typed column.
	out["args_canonical"] = json.loads(bytes(row["args_canonical"]).decode("utf-8"))
	return out


def fetch_operations(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_operation WHERE workflow_id = %s ORDER BY operation_seq",
		          (wf_bytes,))
		rows = c.fetchall()
	out = []
	for row in rows:
		d = _decode_row(row)
		d["input_json"] = _decode_json_field(row, "input_json")
		d["result_json"] = _decode_json_field(row, "result_json")
		out.append(d)
	return out


def fetch_calls(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_call WHERE workflow_id = %s ORDER BY operation_seq",
		          (wf_bytes,))
		rows = c.fetchall()
	return [_decode_row(row) for row in rows]


def fetch_checkpoints(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_workflow_checkpoint WHERE workflow_id = %s ORDER BY seq",
		          (wf_bytes,))
		rows = c.fetchall()
	out = []
	for row in rows:
		d = _decode_row(row)
		d["payload"] = _decode_json_field(row, "payload")
		d["reverse_input_json"] = _decode_json_field(row, "reverse_input_json")
		out.append(d)
	return out


def fetch_events(conn, wf_bytes: bytes):
	with conn.cursor() as c:
		c.execute("SELECT * FROM tb_mf_workflow_event WHERE workflow_id = %s ORDER BY event_ts",
		          (wf_bytes,))
		rows = c.fetchall()
	out = []
	for row in rows:
		d = _decode_row(row)
		d["payload"] = _decode_json_field(row, "payload")
		out.append(d)
	return out


def inspect_workflow(conn, workflow_id_hex: str, max_depth: int, depth: int = 0, seen=None):
	if seen is None:
		seen = set()
	wf_bytes = _hex_bytes(workflow_id_hex, "workflow_id")
	workflow = fetch_workflow(conn, wf_bytes)
	if workflow is None:
		return {"workflow_id": workflow_id_hex, "error": "not_found"}

	node = {
		"workflow_id": workflow_id_hex,
		"workflow": workflow,
		"plan": fetch_plan(conn, wf_bytes),
		"args": fetch_args(conn, wf_bytes),
		"operations": fetch_operations(conn, wf_bytes),
		"calls": fetch_calls(conn, wf_bytes),
		"checkpoints": fetch_checkpoints(conn, wf_bytes),
		"events": fetch_events(conn, wf_bytes),
	}

	children = []
	# seen guards the INSPECTOR's own recursion against a corrupted/cyclic DB; the runtime's own
	# recursion guard (ancestor-set + max_call_depth) is what prevents this in a healthy system.
	seen = seen | {workflow_id_hex}
	for call in node["calls"]:
		child_hex = call["child_workflow_id"]
		if child_hex in seen:
			children.append({"child_workflow_id": child_hex, "cycle_detected": True})
			continue
		if depth + 1 > max_depth:
			children.append({"child_workflow_id": child_hex, "truncated": True})
			continue
		children.append(inspect_workflow(conn, child_hex, max_depth, depth=depth + 1, seen=seen))
	node["children"] = children
	return node


# ===== operator-question endpoints (work/viz-consolidation slice 2) =====

_TERMINAL_STATES = {4, 5, 6, 7}  # completed, reversed, resolved_exception, failed

# tb_mf_workflow_checkpoint.reversal_state (schema comment): 1=forward-held,
# 2=reversal dispatched, 3=resolution_required (unwind blocked), 4=reversed.
CHECKPOINT_RESOLUTION_REQUIRED = 3


def _fetch_workflow_skeleton(conn, wf_bytes: bytes):
	"""The narrow per-node projection the tree/stuck walks use (not SELECT *)."""
	with conn.cursor() as c:
		c.execute(
			"SELECT workflow_id, script_name, state, execution_direction, current_disposition, "
			"terminal_reason, parent_workflow_id, call_depth, lease_owner, lease_expires_at, "
			"next_attempt_at, current_operation_attempt, updated_at "
			"FROM tb_mf_workflow WHERE workflow_id = %s", (wf_bytes,))
		return c.fetchone()


def _fetch_child_ids(conn, wf_bytes: bytes):
	"""Ordered (operation_seq, child_workflow_id hex) pairs from the call sidecar."""
	with conn.cursor() as c:
		c.execute("SELECT operation_seq, child_workflow_id FROM tb_mf_call "
		          "WHERE workflow_id = %s ORDER BY operation_seq", (wf_bytes,))
		return [(row["operation_seq"], bytes(row["child_workflow_id"]).hex())
		        for row in c.fetchall()]


def tree_workflow(conn, workflow_id_hex: str, max_depth: int, depth: int = 0, seen=None):
	"""Skeletal call tree: who called whom, and each node's identity/state — no
	operations/events/payloads (that is `inspect_workflow`). Same recursion
	semantics as inspect: max_depth expands call_depth <= N fully, deeper
	children become explicit {"truncated": true} stubs, and a corrupted-cycle
	guard stubs {"cycle_detected": true}."""
	if seen is None:
		seen = set()
	wf_bytes = _hex_bytes(workflow_id_hex, "workflow_id")
	row = _fetch_workflow_skeleton(conn, wf_bytes)
	if row is None:
		return {"workflow_id": workflow_id_hex, "error": "not_found"}

	node = {
		"workflow_id": workflow_id_hex,
		"parent_workflow_id": _decode_value(row["parent_workflow_id"]),
		"depth": row["call_depth"] if row["call_depth"] is not None else 0,
		"script_name": row["script_name"],
		"state": row["state"],
		"state_name": STATE_NAMES.get(row["state"]),
		"current_disposition": row["current_disposition"],
		"terminal_reason": row["terminal_reason"],
	}

	children = []
	seen = seen | {workflow_id_hex}
	for _seq, child_hex in _fetch_child_ids(conn, wf_bytes):
		if child_hex in seen:
			children.append({"child_workflow_id": child_hex, "cycle_detected": True})
			continue
		if depth + 1 > max_depth:
			children.append({"child_workflow_id": child_hex, "truncated": True})
			continue
		children.append(tree_workflow(conn, child_hex, max_depth, depth=depth + 1, seen=seen))
	node["children"] = children
	return node


def _collect_tree_nodes(conn, workflow_id_hex: str, max_depth: int):
	"""Flatten the tree walk into {hex: {depth, script_name}} (stubs excluded)."""
	nodes: dict[str, dict] = {}

	def walk(node, depth):
		if "error" in node or node.get("truncated") or node.get("cycle_detected"):
			return
		nodes[node["workflow_id"]] = {"depth": depth, "script_name": node["script_name"]}
		for child in node.get("children", []):
			walk(child, depth + 1)

	root = tree_workflow(conn, workflow_id_hex, max_depth)
	if "error" in root:
		return root, nodes
	walk(root, 0)
	return root, nodes


def timeline_workflow(conn, workflow_id_hex: str, max_depth: int):
	"""'What ran, in what order?' — the merged event history of the whole tree.

	Response order is event_ts CHRONOLOGY: per workflow, event_ts is the
	strictly-monotonic ordering key (enforced at append time); across workflows,
	equal timestamps are tie-broken deterministically by workflow_id (an internal
	tie-breaker only — not an ordering concept the API exposes). Each entry
	carries its owning workflow_id plus that node's depth/script_name relative
	to the requested root."""
	root, nodes = _collect_tree_nodes(conn, workflow_id_hex, max_depth)
	if "error" in root:
		return root
	id_bytes = [bytes.fromhex(h) for h in nodes]
	placeholders = ", ".join(["%s"] * len(id_bytes))
	with conn.cursor() as c:
		c.execute(
			"SELECT workflow_id, event_ts, kind, actor, request_id, payload "
			f"FROM tb_mf_workflow_event WHERE workflow_id IN ({placeholders}) "
			"ORDER BY event_ts, workflow_id", id_bytes)
		rows = c.fetchall()
	events = []
	for row in rows:
		d = _decode_row(row)
		d["payload"] = _decode_json_field(row, "payload")
		info = nodes[d["workflow_id"]]
		d["depth"] = info["depth"]
		d["script_name"] = info["script_name"]
		events.append(d)
	return {
		"workflow_id": workflow_id_hex,
		"workflows": nodes,
		"events": events,
	}


def _fetch_attention_rows(conn, wf_bytes: bytes):
	"""Operation/checkpoint rows relevant to a stuck verdict: pending forward ops,
	reconcile/redispatch carriers, and non-forward-held checkpoints."""
	with conn.cursor() as c:
		c.execute(
			"SELECT operation_seq, operation_name, status, reconcile_attempts, "
			"reconcile_first_seen_at, reconcile_last_seen_at, reconcile_reason, "
			"redispatch_first_seen_at, redispatch_last_at, redispatch_count, updated_at "
			"FROM tb_mf_operation WHERE workflow_id = %s AND (status = 1 "
			"OR reconcile_attempts > 0 OR reconcile_first_seen_at IS NOT NULL "
			"OR redispatch_count > 0 OR redispatch_first_seen_at IS NOT NULL) "
			"ORDER BY operation_seq", (wf_bytes,))
		operations = [_decode_row(r) for r in c.fetchall()]
		c.execute(
			"SELECT seq, operation_name, reversal_state, reverse_operation_name, reversed_at, "
			"resolution_event_ts, reconcile_attempts, reconcile_first_seen_at, "
			"reconcile_last_seen_at, reconcile_reason, redispatch_first_seen_at, "
			"redispatch_last_at, redispatch_count, updated_at "
			"FROM tb_mf_workflow_checkpoint WHERE workflow_id = %s AND (reversal_state <> 1 "
			"OR reconcile_attempts > 0 OR reconcile_first_seen_at IS NOT NULL "
			"OR redispatch_count > 0 OR redispatch_first_seen_at IS NOT NULL) "
			"ORDER BY seq", (wf_bytes,))
		checkpoints = [_decode_row(r) for r in c.fetchall()]
	return operations, checkpoints


def _db_now(conn):
	with conn.cursor() as c:
		c.execute("SELECT NOW(6) AS now")
		return c.fetchone()["now"]


def stuck_workflow(conn, workflow_id_hex: str, seen=None, now=None):
	"""Derived 'why is this not moving?' verdict with the evidence it was derived
	from — not just raw rows. Verdicts, in classification precedence:

	  terminal            state 4/5/6/7 (nothing left to move)
	  blocked_resolution  state 3 — parked for an operator; checkpoints with
	                      reversal_state=3 (resolution_required) are the evidence
	  running_under_lease an executor holds a live lease right now
	  waiting_on_child    a non-terminal child exists; descends recursively to the
	                      deepest non-terminal descendant (path recorded)
	  redispatch_pending  op/checkpoint pending->re-dispatch escalation timer set
	                      (participant confirmed pending after recovery; outranks
	                      reconcile — it is the escalation of it)
	  reconcile_pending   op/checkpoint route-404 reconcile budget being spent
	  scheduled_retry     claimable, next_attempt_at is in the future
	  claimable_now       claimable and due — waiting only for an executor to pick
	                      it up (if this persists, no executor is scanning)

	All time comparisons use the DATABASE clock (db_now in the response), matching
	the coordinator's own claim-scan semantics."""
	if seen is None:
		seen = set()
	wf_bytes = _hex_bytes(workflow_id_hex, "workflow_id")
	row = _fetch_workflow_skeleton(conn, wf_bytes)
	if row is None:
		return {"workflow_id": workflow_id_hex, "error": "not_found"}
	if now is None:
		now = _db_now(conn)

	operations, checkpoints = _fetch_attention_rows(conn, wf_bytes)
	evidence = {
		"state": row["state"],
		"state_name": STATE_NAMES.get(row["state"]),
		"execution_direction": row["execution_direction"],
		"current_disposition": row["current_disposition"],
		"terminal_reason": row["terminal_reason"],
		"lease_owner": _decode_value(row["lease_owner"]),
		"lease_expires_at": _decode_value(row["lease_expires_at"]),
		"next_attempt_at": _decode_value(row["next_attempt_at"]),
		"current_operation_attempt": row["current_operation_attempt"],
		"updated_at": _decode_value(row["updated_at"]),
	}
	node = {
		"workflow_id": workflow_id_hex,
		"script_name": row["script_name"],
		"db_now": _decode_value(now),  # the one rendering path: …\.\d{6}Z
		"evidence": evidence,
		"operations": operations,
		"checkpoints": checkpoints,
	}

	def done(verdict: str, detail: str, **extra):
		node["verdict"] = verdict
		node["detail"] = detail
		node.update(extra)
		return node

	if row["state"] in _TERMINAL_STATES:
		return done("terminal",
		            f"workflow is terminal ({STATE_NAMES.get(row['state'])}); nothing is running "
		            + (f"— terminal_reason: {row['terminal_reason']}" if row["terminal_reason"]
		               else "and nothing is awaited"))

	if row["state"] == 3:
		res = [cp for cp in checkpoints
		       if cp["reversal_state"] == CHECKPOINT_RESOLUTION_REQUIRED]
		return done("blocked_resolution",
		            "parked for an operator: reversal hit a nonretryable failure "
		            f"({len(res)} checkpoint(s) in resolution_required); no retry/timer will "
		            "move this — it needs an authorized resolution",
		            resolution_required=res)

	lease_live = (row["lease_owner"] is not None
	              and row["lease_expires_at"] is not None
	              and row["lease_expires_at"] > now)
	if lease_live:
		return done("running_under_lease",
		            f"an executor ({evidence['lease_owner']}) holds a live lease until "
		            f"{evidence['lease_expires_at']} — the workflow is being driven right now")

	# Waiting on a child: any call whose child workflow is non-terminal. Descend
	# to the deepest non-terminal descendant — that node is where the answer is.
	seen = seen | {workflow_id_hex}
	for _seq, child_hex in _fetch_child_ids(conn, wf_bytes):
		if child_hex in seen:
			continue  # corrupted-cycle guard; the runtime's own guard prevents this
		child_bytes = _hex_bytes(child_hex, "child_workflow_id")
		child_row = _fetch_workflow_skeleton(conn, child_bytes)
		if child_row is not None and child_row["state"] not in _TERMINAL_STATES:
			child_verdict = stuck_workflow(conn, child_hex, seen=seen, now=now)
			# When the child is itself waiting_on_child it carries its own path
			# (starting with child_hex); otherwise the path ends at the child.
			path = [workflow_id_hex] + child_verdict.pop("path", [child_hex])
			return done("waiting_on_child",
			            f"pending on child workflow {child_hex}; deepest non-terminal "
			            f"descendant is {path[-1]} — see waiting_on for its verdict",
			            waiting_on=child_verdict, path=path)

	def _recovery_rows(prefix, count_col):
		return [r for r in operations + checkpoints
		        if r[count_col] or r[f"{prefix}_first_seen_at"] is not None]

	redisp = _recovery_rows("redispatch", "redispatch_count")
	if redisp:
		return done("redispatch_pending",
		            "a participant confirmed the operation is still in progress after recovery; "
		            "the durable pending->re-dispatch escalation timer is armed "
		            f"({len(redisp)} row(s)) — uflowsd will re-PUT (byte-identical) to trigger "
		            "the participant's reclaim path",
		            redispatch=redisp)

	reconc = _recovery_rows("reconcile", "reconcile_attempts")
	if reconc:
		return done("reconcile_pending",
		            "the participant route is confirmed missing (route-404); the bounded durable "
		            f"reconcile budget is being spent ({len(reconc)} row(s)) — when it runs out "
		            "the workflow parks as blocked for an operator",
		            reconcile=reconc)

	if row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
		return done("scheduled_retry",
		            f"claimable but not yet due: next_attempt_at {evidence['next_attempt_at']} "
		            f"is in the future (db_now {node['db_now']}); an executor will pick it up then")

	return done("claimable_now",
	            "due and unclaimed: no live lease, next_attempt_at has passed — an executor "
	            "should claim this on its next scan; if this verdict persists, no executor is "
	            "scanning this script")


# ===== bounded search/list (golden-pinned JSON contract) =====

def list_workflows(conn, script: str, since: str, until: str,
                   plan_version: str | None = None, state: str | None = None):
	"""Search tb_mf_workflow (+ tb_mf_workflow_plan for plan_version) by script + a bounded
	created_at range (all three required by the caller, to rule out an accidental full-table
	scan) plus optional plan_version/state. Returns a summary dict per matching row -- never a
	tree (use inspect_workflow for that).

	Summary field set (golden-pinned): workflow_id, script_name, plan_version,
	state/state_name/execution_direction/current_disposition, parent/root ids,
	created_at, updated_at, current_event_ts, terminal_reason."""
	where = ["w.script_name = %s", "w.created_at >= %s", "w.created_at <= %s"]
	params: list = [script, since, until]

	if plan_version is not None:
		where.append("p.plan_version = %s")
		params.append(plan_version)
	state_code = resolve_state(state)
	if state_code is not None:
		where.append("w.state = %s")
		params.append(state_code)

	sql = (
		"SELECT w.workflow_id, w.script_name, p.plan_version, w.state, w.execution_direction, "
		"w.current_disposition, w.created_at, w.updated_at, w.current_event_ts, "
		"w.parent_workflow_id, w.root_workflow_id, w.terminal_reason "
		"FROM tb_mf_workflow w LEFT JOIN tb_mf_workflow_plan p ON p.workflow_id = w.workflow_id "
		"WHERE " + " AND ".join(where) + " ORDER BY w.created_at DESC"
	)

	with conn.cursor() as c:
		c.execute(sql, params)
		rows = c.fetchall()

	results = []
	for row in rows:
		d = _decode_row(row)
		d["state_name"] = STATE_NAMES.get(row["state"])
		results.append(d)
	return results
