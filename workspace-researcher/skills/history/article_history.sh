#!/usr/bin/env bash
# article_history.sh -- 7-day per-project URL dedup for the news pipeline.
#
# Usage:
#   article_history.sh check <url> [--project <slug>]
#   article_history.sh add   <url> [--project <slug>]
#   article_history.sh check-batch [--project <slug>] [--stdin] [url ...]
#
# check-batch: prints URLs that EXIST (one per line). Uses batched SQLite via history_batch.py.

set -euo pipefail

DB_PATH="${ARTICLE_HISTORY_DB:-/home/bhard/.openclaw/article_history.db}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BATCH_PY="${SCRIPT_DIR}/history_batch.py"

PROJECT_SLUG_DEFAULT="${PROJECT_SLUG:-coinography}"

ACTION=""
URL=""
PROJECT="$PROJECT_SLUG_DEFAULT"
USE_STDIN=0
URLS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    check|add|check-batch)
      ACTION="$1"; shift ;;
    --project)
      PROJECT="$2"; shift 2 ;;
    --stdin)
      USE_STDIN=1; shift ;;
    *)
      URLS+=("$1")
      if [[ -z "$URL" ]]; then URL="$1"; fi
      shift ;;
  esac
done

PROJECT="${PROJECT:-coinography}"
ACTIVE_URL_FILE="/tmp/${PROJECT}-active-url.txt"
LEGACY_ACTIVE_URL_FILE="/tmp/openclaw_active_url.txt"

_ensure_schema_once() {
  sqlite3 "$DB_PATH" <<SQL
CREATE TABLE IF NOT EXISTS history (
  url TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT 'coinography',
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (url, project)
);
CREATE INDEX IF NOT EXISTS idx_history_project_ts ON history(project, timestamp);
SQL
}

_purge_old_once() {
  sqlite3 "$DB_PATH" "DELETE FROM history WHERE timestamp <= datetime('now', '-7 days');"
}

if [[ "$ACTION" == "check-batch" ]]; then
  _ensure_schema_once
  _purge_old_once
  export ARTICLE_HISTORY_DB="$DB_PATH"
  if [[ "$USE_STDIN" == "1" ]]; then
    python3 "$BATCH_PY" check-batch --project "$PROJECT" --stdin
  else
    python3 "$BATCH_PY" check-batch --project "$PROJECT" "${URLS[@]}"
  fi
  exit 0
fi

_ensure_schema_once
_purge_old_once

if [[ -z "$ACTION" ]]; then
  echo "Usage: $0 check <url> [--project <slug>]"
  echo "       $0 add   <url> [--project <slug>]"
  echo "       $0 check-batch [--project <slug>] [--stdin] [url ...]"
  exit 2
fi

if [[ -z "$URL" ]]; then
  echo "$ACTION: missing <url>"
  exit 2
fi

SAFE_URL="${URL//\'/\'\'}"
SAFE_PROJECT="${PROJECT//\'/\'\'}"

case "$ACTION" in
  check)
    COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM history WHERE url='${SAFE_URL}' AND project='${SAFE_PROJECT}';")
    if [[ "$COUNT" -gt 0 ]]; then
      echo "EXISTS"
    else
      echo "$URL" > "$ACTIVE_URL_FILE"
      echo "$URL" > "$LEGACY_ACTIVE_URL_FILE"
      echo "NOT_FOUND"
    fi
    ;;
  add)
    if [[ -n "$SAFE_URL" ]]; then
      sqlite3 "$DB_PATH" "INSERT OR IGNORE INTO history (url, project) VALUES ('${SAFE_URL}', '${SAFE_PROJECT}');"
    fi
    echo "ADDED"
    ;;
esac
