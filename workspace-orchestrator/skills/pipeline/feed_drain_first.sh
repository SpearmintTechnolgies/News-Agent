#!/usr/bin/env bash
# Windows-safe FEED_DRAIN first step. No caller-supplied $vars.
# Reads job id from /tmp/<project>-feed-job-id.txt (written by _fire_drain_once.py).
set -euo pipefail
# WSL bash.exe is broken on this machine; Git Bash must win for python3/curl.
if [[ -x "/c/Program Files/Git/bin/bash.exe" ]]; then
  export PATH="/c/Program Files/Git/bin:/c/Program Files/Git/usr/bin:$PATH"
elif [[ -x "/c/Program Files/Git/usr/bin/bash.exe" ]]; then
  export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:$PATH"
fi
PROJECT="${1:-coinnetwork}"
ID_FILE="/tmp/${PROJECT}-feed-job-id.txt"
if [[ ! -f "$ID_FILE" ]]; then
  echo "FEED_DRAIN_FIRST_ERROR: missing $ID_FILE" >&2
  exit 1
fi
JOB_ID="$(tr -d '[:space:]' < "$ID_FILE")"
if [[ -z "$JOB_ID" ]]; then
  echo "FEED_DRAIN_FIRST_ERROR: empty job id" >&2
  exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/bootstrap_feed_drain.py" --feed-job-id "$JOB_ID" --project "$PROJECT"
