#!/usr/bin/env python3
"""Windows-safe launcher for pool_scheduler.py. Idempotent; never starts a duplicate."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
SCHEDULER = os.path.join(HERE, "pool_scheduler.py")
LOG_DIR = os.path.expanduser("~/.openclaw/logs")
LOG_FILE = os.path.join(LOG_DIR, "pool-scheduler.log")


def _already_running() -> bool:
    if os.name != "nt":
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", "python.*pool_scheduler.py"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            return bool(out.strip())
        except (OSError, subprocess.CalledProcessError):
            return False
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'pool_scheduler.py' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        return any(line.strip().isdigit() for line in out.splitlines())
    except Exception:
        return False


def main() -> int:
    os.environ.setdefault("TZ", "Asia/Kolkata")
    os.environ.setdefault("SCAN_EVERY_MIN", "30")
    os.environ.setdefault("FEED_EVERY_MIN", "180")
    os.environ.setdefault("FEED_QUIET_START_HOUR", "0")
    os.environ.setdefault("FEED_QUIET_END_HOUR", "6")
    os.environ.setdefault("FEED_QUIET_TIMEZONE", "Asia/Kolkata")
    os.environ.setdefault("DISPATCH_EVERY_MIN", "1")

    if _already_running():
        print("pool_scheduler already running")
        return 0

    os.makedirs(LOG_DIR, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")
    env = os.environ.copy()
    flags = 0
    if os.name == "nt":
        flags = 0x00000200 | 0x00000008 | 0x08000000  # NEW_GROUP|DETACHED|NO_WINDOW
    proc = subprocess.Popen(
        [sys.executable, "-u", SCHEDULER],
        cwd=HERE,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=flags,
        close_fds=False,
    )
    print(
        "started pool_scheduler.py "
        f"(TZ={env.get('TZ')} pid={proc.pid})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
