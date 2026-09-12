#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DB="${SCRIPT_DIR}/openclaw-data/data/editorial.db"
PROJECTS_DIR="${SCRIPT_DIR}/openclaw-data/projects"
ASSETS_DIR="${SCRIPT_DIR}/openclaw-data/assets"
FAIL=0

echo "=== News Agent Docker verify ==="

if [[ ! -f "$DB" ]]; then
  echo "FAIL: $DB not found — run ./export-data.sh"
  exit 1
fi

POOL="$(sqlite3 "$DB" "SELECT COUNT(*) FROM headline_pool;" 2>/dev/null || echo "?")"
ARTICLES="$(sqlite3 "$DB" "SELECT COUNT(*) FROM articles;" 2>/dev/null || echo "?")"
echo "DB headline_pool: ${POOL}"
echo "DB articles:      ${ARTICLES}"

# Project logos on disk (host openclaw-data)
if [[ -d "$PROJECTS_DIR" ]]; then
  for proj in "$PROJECTS_DIR"/*.json; do
    [[ -f "$proj" ]] || continue
    base="$(basename "$proj" .json)"
    [[ "$base" == _* ]] && continue
    [[ "$base" == presets ]] && continue
    logo_rel="$(python3 - "$proj" <<'PY' 2>/dev/null || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
print((d.get("creator") or {}).get("logo_path") or "")
PY
)"
    if [[ -z "$logo_rel" ]]; then
      echo "WARN: ${base}: no creator.logo_path in project JSON"
      continue
    fi
    logo_host="${ASSETS_DIR}/${logo_rel#assets/}"
    if [[ -f "$logo_host" ]]; then
      echo "logo ${base}: OK (${logo_rel})"
    else
      echo "FAIL: logo missing for ${base}: expected ${logo_host}"
      FAIL=1
    fi
  done
fi

if docker compose ps --status running 2>/dev/null | grep -q news-agent; then
  echo "Container: running"
  if docker compose exec -T news-agent pgrep -f pool_scheduler.py >/dev/null 2>&1; then
    echo "pool_scheduler: OK"
  else
    echo "FAIL: pool_scheduler not running"
    FAIL=1
  fi
  if docker compose exec -T news-agent curl -sf http://127.0.0.1:18789/ >/dev/null 2>&1; then
    echo "gateway: OK"
  elif docker compose exec -T news-agent pgrep -f openclaw-gateway >/dev/null 2>&1; then
    echo "gateway: starting (process up, UI may take ~60s on first boot)"
  else
    echo "WARN: gateway not running"
  fi
  if docker compose exec -T news-agent sh -c 'command -v convert >/dev/null' 2>&1; then
    echo "imagemagick (convert): OK"
  else
    echo "FAIL: ImageMagick convert missing — logo watermark will fail"
    FAIL=1
  fi
  IMG_MODEL="$(docker compose exec -T news-agent printenv IMAGE_MODEL 2>/dev/null | tr -d '\r' || true)"
  if [[ -n "$IMG_MODEL" ]]; then
    echo "IMAGE_MODEL: ${IMG_MODEL}"
  else
    echo "IMAGE_MODEL: (using generate.sh default — set in .env to override)"
  fi
  if docker compose exec -T news-agent python3 \
      /home/openclaw/.openclaw/workspace-orchestrator/skills/pipeline/sync_openclaw_from_projects.py \
      --dry-run >/dev/null 2>&1; then
    echo "telegram sync dry-run: OK"
  else
    echo "WARN: telegram sync dry-run failed (check projects + openclaw.json)"
  fi
else
  echo "WARN: container not running — start with: docker compose up -d"
fi

echo "=== done ==="
exit $FAIL
