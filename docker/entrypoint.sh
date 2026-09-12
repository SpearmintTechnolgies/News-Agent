#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_ROOT="${HOME}/.openclaw"
PIPELINE="${OPENCLAW_ROOT}/workspace-orchestrator/skills/pipeline"
LOG_DIR="${OPENCLAW_ROOT}/logs"

mkdir -p "$LOG_DIR"

log() { echo "[entrypoint $(date -Is)] $*"; }

patch_bifrost_url() {
  local url="${BIFROST_BASE_URL:-http://bifrost:8080/v1}"
  export BIFROST_BASE_URL="$url"
  local cfg="${OPENCLAW_ROOT}/openclaw.json"
  python3 - "$cfg" "$url" <<'PY'
import json
import sys

path, url = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("env", {})["BIFROST_BASE_URL"] = url
data["models"]["providers"]["local-bifrost"]["baseUrl"] = url
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  log "BIFROST_BASE_URL=${url} (patched openclaw.json)"
}

seed_assets_if_needed() {
  local assets="${OPENCLAW_ROOT}/assets"
  local seed="/home/openclaw/.openclaw-asset-seed"
  local inbound="${OPENCLAW_ROOT}/media/inbound"
  mkdir -p "$assets" "$inbound" "${OPENCLAW_ROOT}/.backups/openclaw-json"
  if [[ -d "$seed" ]]; then
    local f base
    for f in "$seed"/*; do
      [[ -f "$f" ]] || continue
      base="$(basename "$f")"
      if [[ ! -f "${assets}/${base}" ]]; then
        cp -a "$f" "${assets}/${base}"
        log "seeded asset: ${base}"
      fi
    done
  fi
}

sync_telegram_from_projects() {
  if [[ ! -f "${PIPELINE}/sync_openclaw_from_projects.py" ]]; then
    log "WARNING: sync_openclaw_from_projects.py missing — skipping telegram sync"
    return 0
  fi
  log "Syncing openclaw.json telegram bindings from projects/*.json..."
  if python3 "${PIPELINE}/sync_openclaw_from_projects.py" --apply --skip-restart; then
    log "telegram sync OK"
  else
    log "WARNING: telegram sync failed — check projects/*.json and openclaw.json"
  fi
}

verify_mounts() {
  local editorial="${OPENCLAW_ROOT}/data/editorial.db"
  if [[ ! -f "$editorial" ]]; then
    log "ERROR: ${editorial} missing — run ./export-data.sh first"
    exit 1
  fi
  local count
  count="$(sqlite3 "$editorial" "SELECT COUNT(*) FROM headline_pool;" 2>/dev/null || echo "?")"
  log "editorial.db OK — headline_pool rows: ${count}"
  if [[ -f "${OPENCLAW_ROOT}/article_history.db" ]]; then
    log "article_history.db OK"
  fi
}

setup_auth_env() {
  export GOG_KEYRING_PASSWORD="${GOG_KEYRING_PASSWORD:-}"
  export GOG_ACCOUNT="${GOG_ACCOUNT:-}"
  export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/home/openclaw/.config}"

  if [[ -d "${XDG_CONFIG_HOME}/gogcli/keyring" ]]; then
    if gog auth list --no-input 2>/dev/null; then
      log "gog auth OK"
    else
      log "WARNING: gog auth check failed — verify GOG_KEYRING_PASSWORD and gogcli-config mount"
    fi
  else
    log "WARNING: gogcli keyring not found at ${XDG_CONFIG_HOME}/gogcli/keyring"
  fi
}

start_gateway() {
  log "Starting openclaw gateway (background)..."
  mkdir -p "$LOG_DIR"
  openclaw gateway >>"${LOG_DIR}/gateway.log" 2>&1 &
  echo "$!" > "${LOG_DIR}/gateway.pid" 2>/dev/null || true
}

start_scheduler() {
  log "Starting pool_scheduler..."
  cd "$PIPELINE"
  exec python3 -u pool_scheduler.py
}

shutdown() {
  log "Shutting down..."
  [[ -f "${LOG_DIR}/gateway.pid" ]] && kill "$(cat "${LOG_DIR}/gateway.pid")" 2>/dev/null || true
  pkill -f pool_scheduler.py 2>/dev/null || true
  exit 0
}

trap shutdown SIGTERM SIGINT

if [[ ! -f "${OPENCLAW_ROOT}/openclaw.json" ]]; then
  echo "ERROR: No openclaw.json in ${OPENCLAW_ROOT}"
  exit 1
fi

patch_bifrost_url || log "Bifrost url patch skipped/failed"
verify_mounts
seed_assets_if_needed
sync_telegram_from_projects
setup_auth_env
start_gateway
start_scheduler
