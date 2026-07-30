#!/usr/bin/env bash
# Idempotent launcher for pool_scheduler.py.
# Safe to run repeatedly; never starts a duplicate process.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${HOME}/.openclaw/logs"
LOG_FILE="${LOG_DIR}/pool-scheduler.log"
# Run the scheduler in local time for log timestamps; interval jobs use monotonic timers.
export TZ="${TZ:-Asia/Kolkata}"
export SCAN_EVERY_MIN="${SCAN_EVERY_MIN:-30}"
export FEED_EVERY_MIN="${FEED_EVERY_MIN:-180}"
export FEED_QUIET_START_HOUR="${FEED_QUIET_START_HOUR:-0}"
export FEED_QUIET_END_HOUR="${FEED_QUIET_END_HOUR:-6}"
export FEED_QUIET_TIMEZONE="${FEED_QUIET_TIMEZONE:-Asia/Kolkata}"
export DISPATCH_EVERY_MIN="${DISPATCH_EVERY_MIN:-1}"

if pgrep -f "[p]ython3.*pool_scheduler.py" >/dev/null 2>&1; then
  echo "pool_scheduler already running"
  exit 0
fi

mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"
setsid nohup python3 pool_scheduler.py >>"$LOG_FILE" 2>&1 </dev/null &
echo "started pool_scheduler.py (TZ=${TZ} SCAN_EVERY_MIN=${SCAN_EVERY_MIN} FEED_EVERY_MIN=${FEED_EVERY_MIN} FEED_QUIET=${FEED_QUIET_START_HOUR}-${FEED_QUIET_END_HOUR} DISPATCH_EVERY_MIN=${DISPATCH_EVERY_MIN}, pid=$!)"
