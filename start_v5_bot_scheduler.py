"""Logon launcher for Task Scheduler task NewsAgentV5.

Runs start_v5_bot.py with pythonw (no console) and appends stdout/stderr
to logs/v5_bot_scheduler.log. Not an application-source change.
"""
from __future__ import annotations

import os
import runpy
import sys
from datetime import datetime
from pathlib import Path

repo = Path(__file__).resolve().parent
os.chdir(repo)
log_path = repo / "logs" / "v5_bot_scheduler.log"
log_path.parent.mkdir(exist_ok=True)
log = open(log_path, "a", encoding="utf-8", buffering=1)
log.write(f"\n[{datetime.now().isoformat()}] scheduler start\n")
log.flush()
sys.stdout = log
sys.stderr = log
sys.path.insert(0, str(repo / "src"))
runpy.run_path(str(repo / "start_v5_bot.py"), run_name="__main__")
