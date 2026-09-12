#!/usr/bin/env python3
"""mark_feed_job.py — Mark a feed_jobs row done/failed and drain the queue.

Called by the orchestrator at the end of a FEED_JOB pipeline run.

Usage:
  python3 mark_feed_job.py --job-id 42 --status done
  python3 mark_feed_job.py --job-id 42 --status failed
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Mark a feed job complete and dispatch next")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--status", choices=("done", "failed"), required=True)
    args = p.parse_args()

    db.init_db()
    job = db.get_feed_job(args.job_id)
    if not job:
        print(f"FEED_JOB_NOT_FOUND: id={args.job_id}", file=sys.stderr)
        return 0

    db.mark_feed_job(args.job_id, args.status)
    print(f"FEED_JOB_MARKED: id={args.job_id} status={args.status}", file=sys.stderr)

    try:
        subprocess.run(
            [sys.executable, os.path.join(HERE, "dispatch_feed_jobs.py")],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as e:
        print(f"DISPATCH_AFTER_MARK_WARN: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
