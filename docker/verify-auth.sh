#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== gog auth verify ==="

if ! docker compose ps --status running 2>/dev/null | grep -q news-agent; then
  echo "FAIL: container not running — docker compose up -d first"
  exit 1
fi

echo "Before restart:"
docker compose exec -T news-agent gog auth list --no-input

echo ""
echo "Restarting container..."
docker compose restart news-agent
sleep 8

echo ""
echo "After restart:"
docker compose exec -T news-agent gog auth list --no-input

echo ""
echo "=== gog auth persisted OK ==="
