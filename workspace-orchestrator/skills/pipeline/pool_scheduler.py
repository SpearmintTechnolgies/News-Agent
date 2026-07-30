#!/usr/bin/env python3
"""pool_scheduler.py — Single long-running scheduler for the approve-title-first flow.

This installed OpenClaw (2026.4.23) has no cron `--command` (no-LLM) job type,
so this one process owns the timed zero-token tasks:

  * scanner    every SCAN_EVERY_MIN (default 30) -> update_headline_pool.py --all
  * feed card  every FEED_EVERY_MIN (default 180) -> send_feed_card.py --all
  * dispatch   every DISPATCH_EVERY_MIN (default 1) -> dispatch_feed_jobs.py
               (crash-recovery only: re-kick drainers with queued work + stale lease)
  * idle watch every IDLE_EVERY_MIN (default 60)  -> check_auto_run.py

During FEED_QUIET_START_HOUR–FEED_QUIET_END_HOUR IST (default 00:00–06:00) ALL
four jobs are paused (no scanner, feed, dispatch, or auto-run). Gateway stays
up; heartbeat quiet hours are configured separately in openclaw.json.

All four are pure Python and burn ZERO model tokens; only check_auto_run wakes
the orchestrator (one LLM run) when the group has been silent >= 48h.

Run it the same way you run the gateway. Recommended (persistent):
  systemd unit at workspace-orchestrator/config/openclaw-pool-scheduler.service
Or quick/background:
  nohup python3 pool_scheduler.py >> ~/.openclaw/logs/pool-scheduler.log 2>&1 &

Env overrides: SCAN_EVERY_MIN, FEED_EVERY_MIN, FEED_QUIET_START_HOUR,
FEED_QUIET_END_HOUR, FEED_QUIET_TIMEZONE, DISPATCH_EVERY_MIN, IDLE_EVERY_MIN,
TICK_SEC, RUN_SCAN_ON_START (1/0).
"""
from __future__ import annotations

import os
os.environ["PATH"] = "/home/bhard/.npm-global/bin:" + os.environ.get("PATH", "")
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import quiet_hours as qh  # noqa: E402

SCAN_EVERY_MIN = int(os.environ.get("SCAN_EVERY_MIN", "30"))
FEED_EVERY_MIN = int(os.environ.get("FEED_EVERY_MIN", "180"))
DISPATCH_EVERY_MIN = int(os.environ.get("DISPATCH_EVERY_MIN", "1"))
IDLE_EVERY_MIN = int(os.environ.get("IDLE_EVERY_MIN", "60"))
TICK_SEC = int(os.environ.get("TICK_SEC", "30"))
RUN_SCAN_ON_START = os.environ.get("RUN_SCAN_ON_START", "1") == "1"

QUIET_LOG_INTERVAL_SEC = int(os.environ.get("QUIET_LOG_INTERVAL_SEC", "3600"))


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def adjust_feed_fire_time(ts: float) -> float:
    """Defer a feed timestamp to quiet end if it falls inside the quiet window."""
    dt = datetime.fromtimestamp(ts, tz=qh.resolve_tz())
    if not qh.in_quiet_hours(dt):
        return ts
    return qh.quiet_end_from(dt).timestamp()


def compute_next_feed_after_send(from_ts: float) -> float:
    return adjust_feed_fire_time(from_ts + FEED_EVERY_MIN * 60)


def initial_next_feed(from_ts: float) -> float:
    """First feed tick after boot (never immediate; respects quiet hours)."""
    return adjust_feed_fire_time(from_ts + FEED_EVERY_MIN * 60)


def run_script(name: str, *args: str) -> None:
    path = os.path.join(HERE, name)
    try:
        res = subprocess.run(
            [sys.executable, path, *args],
            capture_output=True,
            text=True,
            timeout=900,
        )
        out = (res.stderr or "").strip() or (res.stdout or "").strip()
        log(f"{name} {' '.join(args)} -> rc={res.returncode} {out[-300:]}")
    except Exception as e:
        log(f"{name} ERROR: {e}")


def main() -> int:
    log(
        f"pool_scheduler start: scan={SCAN_EVERY_MIN}m feed={FEED_EVERY_MIN}m "
        f"quiet={qh.quiet_hours_label()} "
        f"dispatch={DISPATCH_EVERY_MIN}m idle={IDLE_EVERY_MIN}m tick={TICK_SEC}s"
    )
    now = time.time()
    next_scan = now if RUN_SCAN_ON_START else now + SCAN_EVERY_MIN * 60
    next_feed = initial_next_feed(now)
    next_dispatch = now + DISPATCH_EVERY_MIN * 60
    next_idle = now + IDLE_EVERY_MIN * 60  # don't fire idle-check instantly on boot

    last_quiet_log = 0.0

    while True:
        now = time.time()
        dt = qh.now_quiet_tz()

        if qh.in_quiet_hours(dt):
            if now - last_quiet_log >= QUIET_LOG_INTERVAL_SEC:
                log(f"quiet hours {qh.quiet_hours_label()}: all jobs paused")
                last_quiet_log = now
            if now >= next_feed:
                next_feed = qh.quiet_end_from(dt).timestamp()
            time.sleep(TICK_SEC)
            continue

        if now >= next_scan:
            run_script("update_headline_pool.py", "--all")
            next_scan = now + SCAN_EVERY_MIN * 60

        if now >= next_feed:
            run_script("send_feed_card.py", "--all")
            next_feed = compute_next_feed_after_send(now)

        if now >= next_dispatch:
            run_script("dispatch_feed_jobs.py")
            next_dispatch = now + DISPATCH_EVERY_MIN * 60

        if now >= next_idle:
            run_script("check_auto_run.py")
            next_idle = now + IDLE_EVERY_MIN * 60

        time.sleep(TICK_SEC)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
