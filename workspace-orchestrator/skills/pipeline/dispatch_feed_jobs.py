#!/usr/bin/env python3
"""dispatch_feed_jobs.py — Kick one worker per project when needed.

Pure Python, NO LLM. Reclaims stale running jobs, then for each project with
queued work and no live drainer lease, fires ONE isolated FEED_DRAIN cron.
Each cron processes exactly ONE story and then re-runs this dispatcher to chain
the next story in a fresh cron (see the SOUL "Feed drain entry"). This script
never claims rows.

Always exits 0.
"""
from __future__ import annotations

import json
import os
os.environ["PATH"] = "/home/bhard/.npm-global/bin:" + os.environ.get("PATH", "")
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402

# Backstop: mark stuck running rows failed so a dead drainer cannot block forever.
STALE_JOB_HOURS = float(os.environ.get("FEED_JOB_STALE_HOURS", "0.5"))
DRAINER_LEASE_STALE_MIN = float(os.environ.get("DRAINER_LEASE_STALE_MIN", "15"))


def _fire_drainer(project: str, *, dry_run: bool) -> str:
    if dry_run:
        msg = "FEED_DRAIN " + json.dumps({"project": project}, ensure_ascii=False)
        print(f"DISPATCH_DRY_RUN: {msg}", file=sys.stderr)
        return f"DISPATCH_DRY_RUN: project={project}"

    claimed_job = db.claim_next_feed_job(project=project)
    if not claimed_job:
        return f"DISPATCH_IDLE: project={project}"

    payload = {"project": project, "feed_job_id": claimed_job.id}
    msg = "FEED_DRAIN " + json.dumps(payload, ensure_ascii=False)

    db.set_drainer_lease(project)

    job_name = (
        f"feed-drain-{project}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    )
    cmd = [
        "openclaw",
        "cron",
        "add",
        "--name",
        job_name,
        "--at",
        "15s",
        "--agent",
        "orchestrator",
        "--session",
        "isolated",
        "--message",
        msg,
        "--timeout-seconds",
        "7200",
        "--delete-after-run",
        "--no-deliver",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            db.mark_feed_job(claimed_job.id, "queued")
            db.clear_drainer_lease(project)
            detail = (res.stderr or res.stdout or "").strip()
            print(
                f"DISPATCH_FIRE_FAILED: project={project} {detail}",
                file=sys.stderr,
            )
            return f"DISPATCH_FIRE_FAILED: project={project}"
    except Exception as e:
        db.mark_feed_job(claimed_job.id, "queued")
        db.clear_drainer_lease(project)
        print(
            f"DISPATCH_FIRE_FAILED: project={project} detail={e}",
            file=sys.stderr,
        )
        return f"DISPATCH_FIRE_FAILED: project={project}"

    print(
        f"DISPATCH_FIRED: project={project} cron={job_name} job_id={claimed_job.id}",
        file=sys.stderr,
    )
    return f"DISPATCH_FIRED: project={project}"


def dispatch_once(*, dry_run: bool = False) -> str:
    """Kick at most one drainer per project that has queued work and no live lease."""
    db.init_db()
    reclaimed = db.reclaim_stale_jobs(STALE_JOB_HOURS)
    if reclaimed:
        print(f"DISPATCH_RECLAIMED: count={reclaimed}", file=sys.stderr)

    projects = db.projects_with_queued_jobs()
    if not projects:
        return "DISPATCH_IDLE"

    fired: list[str] = []
    for project in projects:
        if db.drainer_active(project, stale_minutes=DRAINER_LEASE_STALE_MIN):
            continue
        fired.append(_fire_drainer(project, dry_run=dry_run))
        if dry_run:
            break

    if not fired:
        return "DISPATCH_BUSY"
    return " | ".join(fired)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Kick feed drainer workers for projects with queued jobs"
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(dispatch_once(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
