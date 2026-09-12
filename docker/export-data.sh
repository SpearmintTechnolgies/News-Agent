#!/usr/bin/env bash
# Export persistent data (SQLite, credentials, auth) into openclaw-data/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${OPENCLAW_SRC:-${HOME}/.openclaw}"
DEST="${SCRIPT_DIR}/openclaw-data"

echo "Exporting persistent News Agent data..."
echo "  from: ${SRC}"
echo "  to:   ${DEST}"
echo ""

mkdir -p "${DEST}/data" "${DEST}/credentials/wp" "${DEST}/projects" \
  "${DEST}/telegram" "${DEST}/identity" "${DEST}/devices" \
  "${DEST}/logs" "${DEST}/workspace-orchestrator/state" \
  "${DEST}/gogcli-config" "${DEST}/assets" "${DEST}/media/inbound" "${DEST}/backups"

# SQLite databases
if [[ -f "${SRC}/data/editorial.db" ]]; then
  cp -a "${SRC}/data/editorial.db" "${DEST}/data/"
  [[ -d "${SRC}/data" ]] && rsync -a --exclude='*.db' "${SRC}/data/" "${DEST}/data/" 2>/dev/null || true
fi
[[ -f "${SRC}/article_history.db" ]] && cp -a "${SRC}/article_history.db" "${DEST}/"

# Credentials + projects
[[ -d "${SRC}/credentials" ]] && rsync -a "${SRC}/credentials/" "${DEST}/credentials/"
[[ -d "${SRC}/projects" ]] && rsync -a "${SRC}/projects/" "${DEST}/projects/"

# Telegram offsets
[[ -d "${SRC}/telegram" ]] && rsync -a "${SRC}/telegram/" "${DEST}/telegram/"

# OpenClaw device auth
[[ -d "${SRC}/identity" ]] && rsync -a "${SRC}/identity/" "${DEST}/identity/"
[[ -d "${SRC}/devices" ]] && rsync -a "${SRC}/devices/" "${DEST}/devices/"

# Pipeline auxiliary state
[[ -d "${SRC}/workspace-orchestrator/state" ]] && \
  rsync -a "${SRC}/workspace-orchestrator/state/" "${DEST}/workspace-orchestrator/state/"

# Brand logos (watermark) + onboard inbound media
[[ -d "${SRC}/assets" ]] && rsync -a "${SRC}/assets/" "${DEST}/assets/"
[[ -d "${SRC}/media/inbound" ]] && rsync -a "${SRC}/media/inbound/" "${DEST}/media/inbound/"
[[ -d "${SRC}/.backups" ]] && rsync -a "${SRC}/.backups/" "${DEST}/backups/" 2>/dev/null || true

# Google Drive (gog) auth
  if [[ -d "${HOME}/.config/gogcli" ]]; then
  rsync -a "${HOME}/.config/gogcli/" "${DEST}/gogcli-config/"
  chmod -R u=rwX,go=rX "${DEST}/gogcli-config/"
  chmod 700 "${DEST}/gogcli-config/keyring"
  chmod 600 "${DEST}/gogcli-config/keyring/"* 2>/dev/null || true
  chmod 600 "${DEST}/gogcli-config/credentials.json" 2>/dev/null || true
fi

mkdir -p "${DEST}/logs"
rm -rf "${DEST}/logs/"* 2>/dev/null || true
chmod 777 "${DEST}/logs"

echo ""
echo "Exported files:"
ls -lh "${DEST}/data/editorial.db" "${DEST}/article_history.db" 2>/dev/null || true
ls "${DEST}/credentials/wp/" 2>/dev/null || true
ls "${DEST}/assets/" 2>/dev/null || true
ls "${DEST}/gogcli-config/keyring/" 2>/dev/null || true

echo ""
sqlite3 "${DEST}/data/editorial.db" \
  "SELECT 'headline_pool', COUNT(*) FROM headline_pool
   UNION ALL SELECT 'articles', COUNT(*) FROM articles;" 2>/dev/null || echo "(DB check skipped)"

echo ""
echo "Done. Next: cp .env.example .env && docker compose build"
