#!/usr/bin/env python3
"""Validate audit step completed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
from states import WorkflowState  # noqa: E402
from validators._common import fail, load_workflow, ok, state_at_least  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    try:
        row, _ = load_workflow(args.workflow_id, args.db)
    except KeyError:
        return fail(f"workflow not found: {args.workflow_id}")

    if row.state == WorkflowState.ARCHIVED.value:
        return ok(f"audit archived state={row.state}")

    if not state_at_least(row.state, WorkflowState.AUDITED):
        return fail(f"state {row.state} is below AUDITED")

    audit = backlink_db.get_latest_audit(args.workflow_id, db_path=args.db)
    if audit is None:
        return fail("audit row missing")

    try:
        payload = json.loads(audit.audit_json)
    except json.JSONDecodeError:
        return fail("audit_json invalid")

    if not payload.get("placement_type"):
        return fail("audit placement_type missing")

    return ok(
        f"audited placement={audit.placement_type} pass={audit.pass_} state={row.state}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
