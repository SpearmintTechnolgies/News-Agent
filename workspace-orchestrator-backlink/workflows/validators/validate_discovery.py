#!/usr/bin/env python3
"""Validate discovery step completed."""
from __future__ import annotations

import argparse
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
        row, opp = load_workflow(args.workflow_id, args.db)
    except KeyError:
        return fail(f"workflow not found: {args.workflow_id}")

    if not state_at_least(row.state, WorkflowState.DISCOVERED):
        return fail(f"state {row.state} is below DISCOVERED")

    if not opp or not opp.url:
        return fail("opportunity url missing")

    domain = opp.domain or backlink_db.domain_from_url(opp.url)
    if not domain:
        return fail("domain missing")

    return ok(f"discovered url={opp.url} domain={domain} state={row.state}")


if __name__ == "__main__":
    raise SystemExit(main())
