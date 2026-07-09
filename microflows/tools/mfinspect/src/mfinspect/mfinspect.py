#!/usr/bin/env python3
"""mfinspect — read-only workflow/call-tree state dump for Microflows composition (1b.1/1c).

Composition (1b.1) lets a parent sit `pending` on a child, and a blocked descendant deliberately
does not cascade up the call tree (work/workflow-composition/DESIGN.md) -- 1c extends this with
reverse-child compensation, where a parent's reversal can leave it `pending` on a child's own
in-flight compensation instead. Either way, "what is this workflow actually waiting on?" cannot be
answered by looking at one row -- it requires walking `tb_mf_call.child_workflow_id` down the tree.
This tool answers that question directly from the coordinator DB, without hand SQL.

Two actions, because a script/`.mf` name is NOT an instance identity -- many workflow instances can
run the same script, so name-based lookup can only ever produce a list of candidates:

  mfinspect [global args] inspect <workflow_id> [--max-depth N]
      Exact-instance mode: full recursive JSON tree dump for ONE known workflow_id -- the FULL
      durable state/events for that workflow tree, unfiltered (no --since/--until here; use `list`
      to narrow down to a workflow_id first, then `inspect` it for everything).

  mfinspect [global args] list --script NAME --since TS --until TS [--plan-version V] [--state S]
      Search/discovery mode: matching workflow instances as a JSON array of summaries. --script,
      --since, and --until are all REQUIRED -- deliberately, to rule out an accidental full-table
      scan. Pick a workflow_id from the results, then `inspect` it.

Global args (before the action, e.g. `mfinspect --host ... --port ... list --script ...`):
  --host / --port / --user / --password / --password-env / --database / --indent

READ-ONLY BY DESIGN: every query below is a SELECT. This tool never claims, resumes, notifies,
unblocks, or mutates a timer/lease -- it has no write path at all. It is a debugging aid, not a
recovery or operator-action tool.

First slice: JSON output only (a `--tree` human/text renderer is a later slice, not this one).
`list`'s first-slice filter set is deliberately narrow (script + bounded time range, plus
plan-version/state) -- id-based filters (--root-workflow-id, --parent-workflow-id, --operation-id,
--child-workflow-id) and an event-kind/terminal-reason/arg filter were discussed and are a
follow-up, not dropped for lack of value (see work/mfinspect/Progress.md).

`inspect` output shape (one JSON object per workflow node; `children` nests recursively):
  {
    "workflow_id": "<32-hex>",
    "workflow": {...columns from tb_mf_workflow, decoded...},
    "plan": {...tb_mf_workflow_plan row, or null (legacy single-op workflow)...},
    "args": {...decoded tb_mf_workflow_args.args_canonical, or null...},
    "operations": [...tb_mf_operation rows ordered by operation_seq...],
    "calls": [...tb_mf_call rows ordered by operation_seq...],
    "checkpoints": [...tb_mf_workflow_checkpoint rows ordered by seq...],
    "events": [...tb_mf_workflow_event rows ordered by event_ts, the full history...],
    "children": [...recursively inspected child nodes, or a {"child_workflow_id":..., "truncated":
                  true} stub once --max-depth is reached -- never silently omitted...]
  }

`list` output: a bare JSON array of summaries (workflow_id, script_name, plan_version,
state/state_name/direction/disposition, parent/root ids, created_at, latest event timestamp,
terminal_reason) -- never a tree.

Correlation fields (for later log-correlation work, not built here -- see
work/workflow-composition/PROGRESS.md "1c observability/correlation requirement"): every row that
carries workflow_id, operation_seq, operation_id, operation_name, checkpoint seq, child_workflow_id,
parent_workflow_id, or an event kind/timestamp preserves it verbatim in the output, unrenamed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Iterable

try:
	# pyproject.toml is the authoritative version; read it from installed metadata so the two
	# never drift. Falls back when running from an uninstalled checkout.
	__version__ = _pkg_version("mfinspect")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
	__version__ = "0.0.0+source"

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


class MfInspectError(Exception):
	"""Base exception for user-facing errors."""


def _resolve_password(args: argparse.Namespace) -> str:
	if args.password is not None:
		return args.password
	try:
		return os.environ[args.password_env]
	except KeyError as exc:
		raise MfInspectError(
			f"environment variable {args.password_env} is not set "
			"(or pass --password directly)"
		) from exc


def db(host: str, port: int, user: str, password: str, database: str):
	if pymysql is None:  # pragma: no cover - captured at runtime
		raise MfInspectError("PyMySQL is not available in this interpreter")
	return pymysql.connect(host=host, port=port, user=user, password=password,
	                        database=database, autocommit=True, cursorclass=DictCursor)


def _hex_bytes(value_hex: str, field: str) -> bytes:
	try:
		b = bytes.fromhex(value_hex)
	except ValueError as exc:
		raise MfInspectError(f"invalid {field} (not hex): {value_hex!r}") from exc
	if len(b) != 16:
		raise MfInspectError(
			f"invalid {field} (expected 16 bytes / 32 hex chars, got {len(b)} bytes): {value_hex!r}"
		)
	return b


def _resolve_state(value: str | None) -> int | None:
	if value is None:
		return None
	if value.isdigit():
		code = int(value)
		if code not in STATE_NAMES:
			raise MfInspectError(f"unknown --state code {value!r}; valid codes: "
			                      f"{sorted(STATE_NAMES)}")
		return code
	name = value.lower()
	if name not in STATE_CODES:
		raise MfInspectError(f"unknown --state name {value!r}; valid names: "
		                      f"{sorted(STATE_CODES)}")
	return STATE_CODES[name]


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


# ===== inspect action =====

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


# ===== list action =====

def list_workflows(conn, args: argparse.Namespace):
	"""Search tb_mf_workflow (+ tb_mf_workflow_plan for --plan-version) by --script/--since/--until
	(all required, to rule out an accidental full-table scan) plus optional --plan-version/--state.
	Returns a summary dict per matching row -- never a tree (use `inspect` for that)."""
	where = ["w.script_name = %s", "w.created_at >= %s", "w.created_at <= %s"]
	params = [args.script, args.since, args.until]

	if args.plan_version is not None:
		where.append("p.plan_version = %s")
		params.append(args.plan_version)
	state_code = _resolve_state(args.state)
	if state_code is not None:
		where.append("w.state = %s")
		params.append(state_code)

	sql = (
		"SELECT w.workflow_id, w.script_name, p.plan_version, w.state, w.execution_direction, "
		"w.current_disposition, w.created_at, w.current_event_ts, w.parent_workflow_id, "
		"w.root_workflow_id, w.terminal_reason "
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


# ===== CLI =====

def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Read-only inspector for Microflows composition (1b.1/1c) workflow state. "
		            "Never claims, resumes, notifies, unblocks, or mutates anything.")
	parser.add_argument(
		"--version", "-v", action="version", version=f"mfinspect {__version__}",
		help="Print the mfinspect version and exit.",
	)
	# Global DB/output args: come BEFORE the action on the command line, e.g.
	# `mfinspect --host ... --port ... list --script ...`.
	parser.add_argument(
		"--host", default=os.environ.get("DB_HOST", "127.0.0.1"),
		help="MariaDB host to connect to (default: $DB_HOST or 127.0.0.1).",
	)
	parser.add_argument(
		"--port", type=int, default=int(os.environ.get("DB_PORT", "34214")),
		help="MariaDB port to connect to (default: $DB_PORT or 34214).",
	)
	parser.add_argument(
		"--user", default=os.environ.get("DB_USER", "root"),
		help="MariaDB user to authenticate as (default: $DB_USER or root).",
	)
	parser.add_argument(
		"--password", default=None,
		help="MariaDB password, given directly. Mostly for local dev; prefer --password-env so "
		     "the password never appears in shell history or a process listing.",
	)
	parser.add_argument(
		"--password-env", default="MDB_ROOT_PWD",
		help="Environment variable that holds the MariaDB password, used unless --password is "
		     "given directly (default: MDB_ROOT_PWD).",
	)
	parser.add_argument(
		"--database", default=os.environ.get("DB_NAME", "microflows"),
		help="MariaDB schema/database name (default: $DB_NAME or microflows).",
	)
	parser.add_argument("--indent", type=int, default=2, help="JSON indent (0 for compact).")

	subparsers = parser.add_subparsers(dest="command", required=True)

	inspect_parser = subparsers.add_parser(
		"inspect",
		help="Exact-instance mode: full recursive JSON tree dump for one known workflow_id "
		     "(the full durable state/events for that workflow tree, unfiltered).")
	inspect_parser.add_argument("workflow_id", help="32-hex-char workflow_id (16 bytes)")
	inspect_parser.add_argument(
		"--max-depth", type=int, default=DEFAULT_MAX_DEPTH,
		help="Deepest child call_depth to expand fully (matches tb_mf_workflow.call_depth's own "
		     "numbering: 1 = the root's direct children; a child beyond this depth is a "
		     f"{{truncated: true}} stub, not omitted; default {DEFAULT_MAX_DEPTH}).",
	)
	inspect_parser.set_defaults(func=cmd_inspect)

	list_parser = subparsers.add_parser(
		"list",
		help="Search/discovery mode: matching workflow instances as a JSON array of summaries "
		     "(a script/.mf name is not an instance identity -- pick a workflow_id from the "
		     "results, then `inspect` it).")
	list_parser.add_argument("--script", required=True, help="Exact script_name match.")
	list_parser.add_argument(
		"--since", required=True,
		help="Only include workflows with created_at >= this value "
		     "(e.g. '2026-07-01 00:00:00' or '2026-07-01T00:00:00'); created_at is written in the "
		     "same transaction as the workflow's first (\"created\") event.",
	)
	list_parser.add_argument(
		"--until", required=True,
		help="Only include workflows with created_at <= this value. --script/--since/--until are "
		     "all required, deliberately, to rule out an accidental full-table scan.",
	)
	list_parser.add_argument("--plan-version", default=None,
	                          help="Exact plan_version match (e.g. 1.0.0).")
	list_parser.add_argument(
		"--state", default=None,
		help="Filter by state: a numeric code or name (" +
		     ", ".join(f"{code}={name}" for code, name in sorted(STATE_NAMES.items())) + ").",
	)
	list_parser.set_defaults(func=cmd_list)

	return parser


def cmd_inspect(args: argparse.Namespace, conn):
	if args.max_depth < 1:
		raise MfInspectError("--max-depth must be >= 1")
	return inspect_workflow(conn, args.workflow_id, args.max_depth)


def cmd_list(args: argparse.Namespace, conn):
	return list_workflows(conn, args)


def main(argv: Iterable[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(list(argv) if argv is not None else None)

	try:
		password = _resolve_password(args)
		conn = db(args.host, args.port, args.user, password, args.database)
		try:
			output = args.func(args, conn)
		finally:
			conn.close()
	except MfInspectError as exc:
		print(f"mfinspect error: {exc}", file=sys.stderr)
		return 1

	indent = args.indent if args.indent > 0 else None
	json.dump(output, sys.stdout, indent=indent, sort_keys=True)
	sys.stdout.write("\n")
	return 0


def run() -> None:
	sys.exit(main())


if __name__ == "__main__":
	run()
