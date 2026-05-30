#!/usr/bin/env python3
"""Allocate a collision-safe workflow id."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402


def allocate_workflow_id(
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    prefix: str = "WF",
    max_attempts: int = 20,
) -> str:
    for _ in range(max_attempts):
        wid = workflow_manager.new_workflow_id(prefix=prefix)
        if backlink_db.get_workflow(wid, db_path=db_path) is None:
            return wid
    raise RuntimeError("Could not allocate unique workflow id")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a unique workflow id")
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    parser.add_argument("--prefix", default="WF")
    args = parser.parse_args()
    print(allocate_workflow_id(db_path=args.db, prefix=args.prefix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
