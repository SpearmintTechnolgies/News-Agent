#!/usr/bin/env python3
"""check_auto_run.py — 48h idle watchdog for the approve-title-first flow.

Pure Python, NO LLM. Runs as an OpenClaw cron `--command` job (hourly) so it
costs ZERO tokens when nothing is due. Logic:

  * If no group contact recorded yet -> start the clock and do nothing.
  * If the group has been silent < IDLE_HOURS (48h) -> NO_REPLY (nothing to do).
  * If silent >= 48h: for each project compute remaining = MAX_PER_DAY -
    published_today. If a project still has quota AND fresh pool candidates,
    queue an AUTO_RUN for it.

When due, it wakes the orchestrator exactly once by scheduling a one-shot
isolated agent cron job (`openclaw cron add ... --agent orchestrator
--message "AUTO_RUN ..."`). The orchestrator then runs the AUTO path
(picker SELECT -> per-story pipeline, skipping the Step 2.5 gate), posting to
the group. A fire cooldown prevents overlapping runs; the per-day cap
(remaining) keeps it to MAX_PER_DAY/day per project even during long silence.

Always exits 0. Prints NO_REPLY (suppressed) unless it fired.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402
import project_config as pc  # noqa: E402

IDLE_HOURS = 48.0
MAX_PER_DAY = 4
FIRE_COOLDOWN_HOURS = 6.0  # don't re-fire while a batch is likely still running
FIRE_KEY = "last_auto_fire_at"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_since_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="48h idle auto-run watchdog")
    p.add_argument("--idle-hours", type=float, default=float(os.environ.get("AUTO_IDLE_HOURS", IDLE_HOURS)))
    p.add_argument("--max-per-day", type=int, default=int(os.environ.get("AUTO_MAX_PER_DAY", MAX_PER_DAY)))
    p.add_argument("--dry-run", action="store_true", help="Report what would fire; do not wake the orchestrator")
    args = p.parse_args()

    db.init_db()

    idle = db.hours_since_last_contact()
    if idle is None:
        # No baseline yet — start the clock now and wait.
        db.touch_last_contact()
        print("AUTO_BASELINE_SET: clock started", file=sys.stderr)
        print("NO_REPLY")
        return 0

    if idle < args.idle_hours:
        print(f"AUTO_IDLE: {idle:.1f}h < {args.idle_hours:.0f}h — not due", file=sys.stderr)
        print("NO_REPLY")
        return 0

    # Cooldown guard: avoid stacking auto-runs while one is likely in flight.
    fire_age = _hours_since_iso(db.get_state(db.GLOBAL_PROJECT, FIRE_KEY))
    if fire_age is not None and fire_age < FIRE_COOLDOWN_HOURS:
        print(f"AUTO_COOLDOWN: last fire {fire_age:.1f}h ago (< {FIRE_COOLDOWN_HOURS}h)", file=sys.stderr)
        print("NO_REPLY")
        return 0

    # Build the per-project work list (respecting the daily cap + pool stock).
    work: list[dict] = []
    for slug in pc.list_available_projects():
        published = db.published_today(slug)
        remaining = max(0, args.max_per_day - published)
        if remaining <= 0:
            continue
        pool_n = len(db.fresh_pool(slug, limit=remaining))
        if pool_n <= 0:
            continue
        work.append({"project": slug, "count": min(remaining, pool_n)})

    if not work:
        print("AUTO_NONE: idle>=48h but no project has quota+pool", file=sys.stderr)
        print("NO_REPLY")
        return 0

    if args.dry_run:
        print(f"AUTO_RUN_DUE (dry-run): {json.dumps(work)}", file=sys.stderr)
        print("NO_REPLY")
        return 0

    # Wake the orchestrator ONCE with a one-shot isolated agent cron job.
    msg = "AUTO_RUN " + json.dumps({"projects": work}, ensure_ascii=False)
    job_name = f"auto-run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    cmd = [
        "openclaw", "cron", "add",
        "--name", job_name,
        "--at", "+30s",
        "--agent", "orchestrator",
        "--session", "isolated",
        "--message", msg,
        "--timeout-seconds", "7200",
        "--delete-after-run",
        "--no-deliver",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            print(f"AUTO_FIRE_FAILED: {res.stderr.strip() or res.stdout.strip()}", file=sys.stderr)
            print("NO_REPLY")
            return 0
    except Exception as e:
        print(f"AUTO_FIRE_FAILED: {e}", file=sys.stderr)
        print("NO_REPLY")
        return 0

    db.set_state(db.GLOBAL_PROJECT, FIRE_KEY, _now_iso())
    print(f"AUTO_RUN_FIRED: job={job_name} work={json.dumps(work)}", file=sys.stderr)
    print("NO_REPLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
