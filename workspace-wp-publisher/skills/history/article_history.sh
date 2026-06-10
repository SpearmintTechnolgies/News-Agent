#!/usr/bin/env bash
# article_history.sh -- 7-day per-project URL dedup for the news pipeline.
#
# Usage:
#   article_history.sh check <url> [--project <slug>]
#   article_history.sh add   <url> [--project <slug>]
#
# Project resolution (in order):
#   1. --project flag
#   2. $PROJECT_SLUG env var
#   3. "coinography" (backward compat)
#
# Each project has its own dedup namespace: the same URL on coinography and
# memecoinist would NOT be considered a duplicate. The DB stores a single
# table keyed on (url, project).
#
# The active-URL side-channel file used to hand off the chosen URL to the
# WP-Publisher is project-prefixed: /tmp/<project>-active-url.txt. The legacy
# /tmp/openclaw_active_url.txt is also written for backward compat.

set -euo pipefail

DB_PATH="${ARTICLE_HISTORY_DB:-/home/bhard/.openclaw/article_history.db}"

PROJECT_SLUG_DEFAULT="${PROJECT_SLUG:-coinography}"

ACTION=""
URL=""
PROJECT="$PROJECT_SLUG_DEFAULT"

while [[ $# -gt 0 ]]; do
  case "$1" in
    check|add)
      ACTION="$1"; shift ;;
    --project)
      PROJECT="$2"; shift 2 ;;
    *)
      if [[ -z "$URL" ]]; then URL="$1"; fi
      shift ;;
  esac
done

PROJECT="${PROJECT:-coinography}"
ACTIVE_URL_FILE="/tmp/${PROJECT}-active-url.txt"
LEGACY_ACTIVE_URL_FILE="/tmp/openclaw_active_url.txt"

sqlite3 "$DB_PATH" <<SQL
CREATE TABLE IF NOT EXISTS history (
  url TEXT PRIMARY KEY,
  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
SQL

# Additive migration: add `project` column + composite PK if absent.
HAS_PROJECT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM pragma_table_info('history') WHERE name='project';")
if [[ "$HAS_PROJECT" == "0" ]]; then
  sqlite3 "$DB_PATH" <<SQL
ALTER TABLE history ADD COLUMN project TEXT NOT NULL DEFAULT 'coinography';
UPDATE history SET project='coinography' WHERE project IS NULL OR project='';
CREATE INDEX IF NOT EXISTS idx_history_url_project ON history(url, project);
CREATE INDEX IF NOT EXISTS idx_history_project_ts ON history(project, timestamp);
SQL
fi

# Cleanup older than 7 days
sqlite3 "$DB_PATH" "DELETE FROM history WHERE timestamp <= datetime('now', '-7 days');"

if [[ -z "$ACTION" ]]; then
  echo "Usage: $0 check <url> [--project <slug>]"
  echo "       $0 add   <url> [--project <slug>]"
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
