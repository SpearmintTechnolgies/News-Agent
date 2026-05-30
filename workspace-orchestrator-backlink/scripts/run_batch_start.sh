#!/usr/bin/env bash
# run_batch_start.sh — search gate + batch discover + init orchestrator queue (one exec).
set -euo pipefail

ROOT="${BACKLINK_ROOT:-$HOME/.openclaw/workspace-orchestrator-backlink}"
DB="${BACKLINK_DB:-$HOME/.openclaw/data/backlink_agent.db}"
DISCOVER_JSON="/tmp/backlink-discover-$$.json"

python3 "$ROOT/scripts/test_search_backends.py"

python3 "$ROOT/workflows/workflow_driver_cli.py" discover --db "$DB" > "$DISCOVER_JSON"

python3 "$ROOT/scripts/orchestrator_batch.py" init --discover-json "$DISCOVER_JSON" --db "$DB"

rm -f "$DISCOVER_JSON"
