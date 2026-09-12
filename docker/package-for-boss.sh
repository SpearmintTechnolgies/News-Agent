#!/usr/bin/env bash
# See DOCKER_PREP_AND_TRANSFER.md for transfer bundle layout and deploy flow.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEWS_IMAGE="openclaw-news-agent:2026.4.24"
BIFROST_IMAGE="maximhq/bifrost:v1.6.2"
OUT="${SCRIPT_DIR}/news-agent-deploy"

echo "Packaging News Agent + Bifrost for boss PC..."
mkdir -p "$OUT" "${SCRIPT_DIR}/bifrost-data"

echo "Pulling Bifrost image (if needed)..."
if ! docker image inspect "$BIFROST_IMAGE" >/dev/null 2>&1; then
  docker pull "$BIFROST_IMAGE"
fi
if ! docker image inspect "$NEWS_IMAGE" >/dev/null 2>&1; then
  echo "ERROR: ${NEWS_IMAGE} not found — run: docker compose build news-agent"
  exit 1
fi

echo "Saving Docker images (this may take a few minutes)..."
docker save "$NEWS_IMAGE" | gzip > "${OUT}/openclaw-news-agent-image.tar.gz"
docker save "$BIFROST_IMAGE" | gzip > "${OUT}/bifrost-image.tar.gz"

echo "Archiving persistent data..."
tar czf "${OUT}/openclaw-data.tar.gz" -C "$SCRIPT_DIR" openclaw-data

# Boss-facing compose (no build section)
cat > "${OUT}/docker-compose.yml" <<'YAML'
services:
  bifrost:
    image: maximhq/bifrost:v1.6.2
    container_name: bifrost
    restart: unless-stopped
    environment:
      APP_HOST: 0.0.0.0
      APP_PORT: 8080
    volumes:
      - ./bifrost-data:/app/data
    ports:
      - "${BIFROST_UI_PORT:-8888}:8080"

  news-agent:
    image: openclaw-news-agent:2026.4.24
    container_name: openclaw-news-agent
    restart: unless-stopped
    depends_on:
      - bifrost
    env_file:
      - .env
    environment:
      TZ: ${TZ:-Asia/Kolkata}
      HOME: /home/openclaw
      XDG_CONFIG_HOME: /home/openclaw/.config
      BIFROST_BASE_URL: http://bifrost:8080/v1
      GOG_KEYRING_PASSWORD: ${GOG_KEYRING_PASSWORD:-}
      GOG_ACCOUNT: ${GOG_ACCOUNT:-}
      SCAN_EVERY_MIN: ${SCAN_EVERY_MIN:-30}
      FEED_EVERY_MIN: ${FEED_EVERY_MIN:-180}
      FEED_QUIET_START_HOUR: ${FEED_QUIET_START_HOUR:-0}
      FEED_QUIET_END_HOUR: ${FEED_QUIET_END_HOUR:-6}
      FEED_QUIET_TIMEZONE: ${FEED_QUIET_TIMEZONE:-Asia/Kolkata}
      IMAGE_MODEL: ${IMAGE_MODEL:-vertex/gemini-3.1-flash-image}
      IMAGE_MODEL_FALLBACK: ${IMAGE_MODEL_FALLBACK:-vertex/gemini-3.1-flash-lite-image}
    volumes:
      - ./openclaw-data/data:/home/openclaw/.openclaw/data
      - ./openclaw-data/credentials:/home/openclaw/.openclaw/credentials
      - ./openclaw-data/projects:/home/openclaw/.openclaw/projects
      - ./openclaw-data/telegram:/home/openclaw/.openclaw/telegram
      - ./openclaw-data/identity:/home/openclaw/.openclaw/identity
      - ./openclaw-data/devices:/home/openclaw/.openclaw/devices
      - ./openclaw-data/logs:/home/openclaw/.openclaw/logs
      - ./openclaw-data/article_history.db:/home/openclaw/.openclaw/article_history.db
      - ./openclaw-data/workspace-orchestrator/state:/home/openclaw/.openclaw/workspace-orchestrator/state
      - ./openclaw-data/gogcli-config:/home/openclaw/.config/gogcli
      - ./openclaw-data/assets:/home/openclaw/.openclaw/assets
      - ./openclaw-data/media/inbound:/home/openclaw/.openclaw/media/inbound
      - ./openclaw-data/backups:/home/openclaw/.openclaw/.backups
    ports:
      - "${GATEWAY_PORT:-18789}:18789"
YAML

cp "${SCRIPT_DIR}/.env.example" "${OUT}/.env.example"

cat > "${OUT}/deploy.sh" <<'DEPLOY'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit GOG_* values if needed, then re-run: ./deploy.sh"
  exit 1
fi

mkdir -p bifrost-data

echo "Loading Docker images..."
gunzip -c openclaw-news-agent-image.tar.gz | docker load
gunzip -c bifrost-image.tar.gz | docker load

echo "Extracting persistent data..."
tar xzf openclaw-data.tar.gz

echo "Starting News Agent + Bifrost..."
docker compose up -d

echo ""
echo "Deploy complete."
echo ""
echo "First-time setup:"
echo "  1. Open http://localhost:8888 and add your Vertex (or other) LLM providers"
echo "  2. Provider config persists in ./bifrost-data/ across restarts"
echo ""
echo "Check logs:"
echo "  docker compose logs -f"
DEPLOY
chmod +x "${OUT}/deploy.sh"

cp "${SCRIPT_DIR}/deploy.ps1.template" "${OUT}/deploy.ps1"
cp "${SCRIPT_DIR}/start.ps1.template" "${OUT}/start.ps1"

cat > "${OUT}/deploy.bat" <<'BAT'
@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
if errorlevel 1 pause
BAT

cat > "${OUT}/start.bat" <<'BAT'
@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 pause
BAT

cat > "${OUT}/README-WINDOWS.txt" <<'README'
News Agent + Bifrost — Windows deploy (Docker Desktop, no WSL needed)
======================================================================

Requirements:
  - Docker Desktop for Windows installed and RUNNING (whale icon in tray)
  - Copy this entire folder to e.g. C:\news-agent-deploy

Steps:
  1. Copy .env.example to .env
  2. Edit .env in Notepad — set GOG_KEYRING_PASSWORD and GOG_ACCOUNT
  3. Double-click deploy.bat   (FIRST INSTALL ONLY — loads images + extracts data)
  4. Wait for "Deploy complete" (first run loads images; may take several minutes)
  5. Open http://localhost:18789  — OpenClaw UI
  6. Open http://localhost:8888   — Bifrost UI, add Vertex/Gemini providers once

Daily use (after PC reboot):
  Double-click start.bat  — starts Docker Desktop if needed, then containers.
  Do NOT run deploy.bat again (it re-extracts data and can overwrite live DB).

If port 8888 is busy, set BIFROST_UI_PORT=8889 in .env before deploy.

Useful commands (PowerShell in this folder):
  docker compose ps
  docker compose logs -f
  docker compose restart
  docker compose down

Data persists in:
  openclaw-data/   — databases, Telegram, credentials
  bifrost-data/    — LLM provider config

Image models (in .env — defaults baked into image):
  IMAGE_MODEL=vertex/gemini-3.1-flash-image
  IMAGE_MODEL_FALLBACK=vertex/gemini-3.1-flash-lite-image
README

cat > "${OUT}/UPDATE-BOSS-IMAGE-ONLY.txt" <<'UPDATE'
Update news-agent stack (keeps openclaw-data, bifrost-data, .env)
==================================================================

Use when you already deployed and need a new image and/or docker-compose.yml.
Do NOT run deploy.bat — it re-extracts openclaw-data.tar.gz and can overwrite live DB.

1. Copy into your existing deploy folder:
   - openclaw-news-agent-image.tar.gz
   - docker-compose.yml  (required if new volume mounts)
   - start.bat + start.ps1  (daily one-click start after reboot)

2. One-time if upgrading persistence (first time with assets mount):
   mkdir openclaw-data\assets
   docker compose cp news-agent:/home/openclaw/.openclaw/assets/. openclaw-data\assets\

3. Load new image (deploy.ps1 GZipStream section or gunzip | docker load)

4. Merge into .env if missing:
   IMAGE_MODEL=vertex/gemini-3.1-flash-image
   IMAGE_MODEL_FALLBACK=vertex/gemini-3.1-flash-lite-image

5. docker compose up -d   (or double-click start.bat)

6. Verify:
   docker compose exec -T news-agent which convert
   docker compose exec -T news-agent python3 /home/openclaw/.openclaw/workspace-orchestrator/skills/pipeline/sync_openclaw_from_projects.py --dry-run

Data in openclaw-data/ and bifrost-data/ is NOT touched by steps above.
UPDATE

echo ""
echo "Bundle ready at: ${OUT}/"
ls -lh "${OUT}/"
echo ""
echo "Transfer the entire news-agent-deploy/ folder to boss PC (Windows Docker Desktop), then:"
echo "  1. Copy .env.example to .env and edit GOG_* values"
echo "  2. Double-click deploy.bat  (or run deploy.ps1)"
echo "  3. Open http://localhost:8888 and add LLM providers once"
