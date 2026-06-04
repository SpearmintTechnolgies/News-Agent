#!/usr/bin/env python3
"""
update_pick_status.py — CLI wrapper around editorial_db.update_pick_status.

Usage:
    python3 update_pick_status.py --pick-id 12 --status researching
    python3 update_pick_status.py --pick-id 12 --status published \
        --pipeline-run-id 20260603-120000
    python3 update_pick_status.py --pick-id 12 --status failed \
        --failed-reason "writer length repair limit hit"

Exit 0 + prints PICK_STATUS_UPDATED: pick_id=<id> status=<status>
Exit 1 + prints PICK_STATUS_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pick-id", type=int, required=True)
    parser.add_argument(
        "--status",
        required=True,
        choices=sorted(editorial_db.PICK_VALID_STATUSES),
    )
    parser.add_argument("--failed-reason", default=None)
    parser.add_argument("--pipeline-run-id", default=None)
    parser.add_argument("--db-path", default=editorial_db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    pick = editorial_db.get_pick(args.pick_id, db_path=args.db_path)
    if pick is None:
        print(f"PICK_STATUS_ERROR: pick_id={args.pick_id} not found")
        return 1

    try:
        editorial_db.update_pick_status(
            args.pick_id,
            args.status,
            failed_reason=args.failed_reason,
            pipeline_run_id=args.pipeline_run_id,
            db_path=args.db_path,
        )
    except (sqlite3.Error, ValueError) as e:
        print(f"PICK_STATUS_ERROR: {e}")
        return 1

    print(
        f"PICK_STATUS_UPDATED: pick_id={args.pick_id} status={args.status}"
        + (f" failed_reason={args.failed_reason}" if args.failed_reason else "")
        + (f" pipeline_run_id={args.pipeline_run_id}" if args.pipeline_run_id else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
