#!/usr/bin/env bash
# Rebuild news-agent image (ImageMagick + image model fixes) and package for boss PC.
# Requires: Docker Desktop running with WSL integration.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { echo "[rebuild $(date -Is)] $*"; }

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not available. Start Docker Desktop on Windows, wait for the whale icon, then re-run:"
  echo "  cd ~/.openclaw/docker && ./rebuild-and-package.sh"
  exit 1
fi

log "Step 1/6: prepare-build-context"
./prepare-build-context.sh

log "Step 2/6: docker compose build news-agent (may take 10–20 min)"
docker compose build news-agent

log "Step 3/6: restart stack"
docker compose up -d

log "Step 4/6: verify.sh"
./verify.sh

log "Step 5/6: ImageMagick + generate.sh smoke test"
docker compose exec -T news-agent sh -c 'command -v convert >/dev/null'
docker compose exec -T news-agent printenv IMAGE_MODEL IMAGE_MODEL_FALLBACK || true
docker compose exec -T news-agent bash -lc \
  'BIFROST_BASE_URL=http://bifrost:8080/v1 STAMP_LOGO=0 OUTPUT_PATH=/tmp/rebuild-smoke.jpg \
   bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "test bitcoin coin editorial photo"' \
  | tail -15

log "Step 6/6: package + zip to home directory"
./package-for-boss.sh
rm -f "${HOME}/news-agent-deploy.zip"
(cd "${SCRIPT_DIR}" && zip -r "${HOME}/news-agent-deploy.zip" news-agent-deploy/)
ls -lh "${HOME}/news-agent-deploy.zip"

log "Done. Upload ${HOME}/news-agent-deploy.zip to Google Drive."
log "Boss image-only update: see news-agent-deploy/UPDATE-BOSS-IMAGE-ONLY.txt"
