#!/usr/bin/env python3
"""pool_scheduler.py — Single long-running scheduler for the approve-title-first flow.

This installed OpenClaw (2026.4.23) has no cron `--command` (no-LLM) job type,
so this one process owns the three zero-token timed tasks instead:

  * scanner    every SCAN_EVERY_MIN (default 30) -> update_headline_pool.py --all
  * feed card  daily at FEED_HOUR:FEED_MIN local -> send_feed_card.py --all
  * idle watch every IDLE_EVERY_MIN (default 60)  -> check_auto_run.py

All three are pure Python and burn ZERO model tokens; only check_auto_run wakes
the orchestrator (one LLM run) when the group has been silent >= 48h.

Run it the same way you run the gateway. Recommended (persistent):
  systemd unit at workspace-orchestrator/config/openclaw-pool-scheduler.service
Or quick/background:
  nohup python3 pool_scheduler.py >> ~/.openclaw/logs/pool-scheduler.log 2>&1 &

Env overrides: SCAN_EVERY_MIN, IDLE_EVERY_MIN, FEED_HOUR, FEED_MIN, TICK_SEC,
RUN_SCAN_ON_START (1/0).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.realpath(__file__))

SCAN_EVERY_MIN = int(os.environ.get("SCAN_EVERY_MIN", "30"))
IDLE_EVERY_MIN = int(os.environ.get("IDLE_EVERY_MIN", "60"))
FEED_HOUR = int(os.environ.get("FEED_HOUR", "10"))
FEED_MIN = int(os.environ.get("FEED_MIN", "0"))
TICK_SEC = int(os.environ.get("TICK_SEC", "30"))
RUN_SCAN_ON_START = os.environ.get("RUN_SCAN_ON_START", "1") == "1"


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


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
        f"pool_scheduler start: scan={SCAN_EVERY_MIN}m idle={IDLE_EVERY_MIN}m "
        f"feed={FEED_HOUR:02d}:{FEED_MIN:02d} tick={TICK_SEC}s"
    )
    now = time.time()
    next_scan = now if RUN_SCAN_ON_START else now + SCAN_EVERY_MIN * 60
    next_idle = now + IDLE_EVERY_MIN * 60  # don't fire idle-check instantly on boot
    last_feed_date = None  # 'YYYY-MM-DD' of the last day a feed card was sent

    while True:
        now = time.time()
        dt = datetime.now()

        if now >= next_scan:
            run_script("update_headline_pool.py", "--all")
            next_scan = now + SCAN_EVERY_MIN * 60

        today = dt.strftime("%Y-%m-%d")
        if dt.hour == FEED_HOUR and dt.minute >= FEED_MIN and last_feed_date != today:
            run_script("send_feed_card.py", "--all")
            last_feed_date = today

        if now >= next_idle:
            run_script("check_auto_run.py")
            next_idle = now + IDLE_EVERY_MIN * 60

        time.sleep(TICK_SEC)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
