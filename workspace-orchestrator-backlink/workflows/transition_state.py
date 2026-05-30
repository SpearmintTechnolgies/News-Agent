#!/usr/bin/env python3
"""CLI for backlink workflow state management."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402


def cmd_init(_: argparse.Namespace) -> int:
    backlink_db.init_db()
    print(f"OK: initialized {backlink_db.DEFAULT_DB_PATH}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    backlink_db.init_db()
    wid = args.id or workflow_manager.new_workflow_id()
    row = workflow_manager.create(wid)
    print(json.dumps(workflow_manager.workflow_summary(row.workflow_id), indent=2))
    return 0


def cmd_transition(args: argparse.Namespace) -> int:
    row = workflow_manager.transition(
        args.id,
        args.to,
        agent=args.agent,
        error=args.error,
    )
    summary = workflow_manager.workflow_summary(row.workflow_id)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    summary = workflow_manager.workflow_summary(args.id)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    action = workflow_manager.next_action_for(args.id)
    print(action or "(wait — no automated next step)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backlink workflow state CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create database schema")
    p_init.set_defaults(func=cmd_init)

    p_create = sub.add_parser("create", help="Create a new workflow")
    p_create.add_argument("--id", help="Workflow id (auto-generated if omitted)")
    p_create.set_defaults(func=cmd_create)

    p_trans = sub.add_parser("transition", help="Transition workflow state")
    p_trans.add_argument("--id", required=True)
    p_trans.add_argument("--to", required=True, dest="to")
    p_trans.add_argument("--agent", default=None)
    p_trans.add_argument("--error", default=None)
    p_trans.set_defaults(func=cmd_transition)

    p_show = sub.add_parser("show", help="Show workflow summary")
    p_show.add_argument("--id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_next = sub.add_parser("next", help="Print next action for current state")
    p_next.add_argument("--id", required=True)
    p_next.set_defaults(func=cmd_next)

    args = parser.parse_args()
    try:
        return args.func(args)
    except (KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
