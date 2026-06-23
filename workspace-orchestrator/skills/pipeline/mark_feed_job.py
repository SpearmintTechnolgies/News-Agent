#!/usr/bin/env python3
"""mark_feed_job.py — Mark a feed_jobs row done/failed.

Called by the orchestrator at the end of each article inside a FEED_DRAIN loop.
The drainer continues itself — this script does NOT re-dispatch per job.
A safety kick runs only when the project drainer lease is dead (crash recovery).

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

DRAINER_LEASE_STALE_MIN = float(os.environ.get("DRAINER_LEASE_STALE_MIN", "30"))


def _safety_kick_if_stale(project: str) -> None:
    if db.drainer_active(project, stale_minutes=DRAINER_LEASE_STALE_MIN):
        return
    if db.count_queued_jobs(project=project) <= 0:
        return
    try:
        subprocess.run(
            [sys.executable, os.path.join(HERE, "dispatch_feed_jobs.py")],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as e:
        print(f"DISPATCH_SAFETY_KICK_WARN: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Mark a feed job complete")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--status", choices=("done", "failed"), required=True)
    p.add_argument(
        "--no-safety-kick",
        action="store_true",
        help="Skip crash-recovery dispatch (FEED_DRAIN loop handles continuation)",
    )
    args = p.parse_args()

    db.init_db()
    job = db.get_feed_job(args.job_id)
    if not job:
        print(f"FEED_JOB_NOT_FOUND: id={args.job_id}", file=sys.stderr)
        return 0

    db.mark_feed_job(args.job_id, args.status)
    print(f"FEED_JOB_MARKED: id={args.job_id} status={args.status}", file=sys.stderr)

    if not args.no_safety_kick:
        _safety_kick_if_stale(job.project)

    return 0


if __name__ == "__main__":
    sys.exit(main())
