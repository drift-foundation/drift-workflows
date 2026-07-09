"""Read-only coordinator-DB query layer for the microflows-viz backend.

Ported (mostly unchanged, per work/viz-consolidation slice 1) from
microflows/tools/mfinspect/src/mfinspect/mfinspect.py — the connection, decode,
and fetch helpers plus the recursive `inspect_workflow` tree walk. mfinspect is
prototype query logic scheduled for removal once microflows-viz reaches parity;
until then, keep the two in sync if the schema contract changes.

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
    "events": [...tb_mf_workflow_event rows ordered by event_seq, full history...],
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
	return pymysql.connect(host=cfg.host, port=cfg.port, user=cfg.user,
	                       password=cfg.password, database=cfg.database,
	                       autocommit=True, cursorclass=DictCursor)


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
	"""Render a raw DB value JSON-safely: bytes -> lowercase hex, datetime -> ISO 8601."""
	if v is None:
		return None
	if isinstance(v, (bytes, bytearray)):
		return bytes(v).hex()
	if isinstance(v, datetime):
		return v.isoformat()
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


# ===== exact-instance inspection (mfinspect `inspect` parity) =====

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
		c.execute("SELECT * FROM tb_mf_workflow_event WHERE workflow_id = %s ORDER BY event_seq",
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


# ===== bounded search/list (mfinspect `list` parity) =====

def list_workflows(conn, script: str, since: str, until: str,
                   plan_version: str | None = None, state: str | None = None):
	"""Search tb_mf_workflow (+ tb_mf_workflow_plan for plan_version) by script + a bounded
	created_at range (all three required by the caller, to rule out an accidental full-table
	scan) plus optional plan_version/state. Returns a summary dict per matching row -- never a
	tree (use inspect_workflow for that).

	Parity note: the field set is mfinspect `list`'s summary (workflow_id, script_name,
	plan_version, state/state_name/execution_direction/current_disposition, parent/root ids,
	created_at, current_event_ts, terminal_reason) plus one documented additive field,
	`updated_at` (the row's last-write timestamp; mfinspect never exposed it)."""
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
