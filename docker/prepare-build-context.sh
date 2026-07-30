#!/usr/bin/env bash
# See DOCKER_PREP_AND_TRANSFER.md for build context staging and path patching.
# Copy static OpenClaw files into build-context/ with Docker paths pre-patched.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${OPENCLAW_SRC:-${HOME}/.openclaw}"
DEST="${SCRIPT_DIR}/build-context"
CONTAINER_ROOT="/home/openclaw/.openclaw"
GOG_BIN="${SCRIPT_DIR}/gog-bin"

if [[ ! -f "${SRC}/openclaw.json" ]]; then
  echo "ERROR: ${SRC}/openclaw.json not found"
  exit 1
fi

echo "Preparing Docker build context..."
echo "  from: ${SRC}"
echo "  to:   ${DEST}"
echo ""

rm -rf "$DEST"
mkdir -p "$DEST" "$GOG_BIN"

# Static code + config (no DBs, no sessions, no logs)
rsync -a \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='browser/' \
  --exclude='node_modules/' \
  --exclude='agents/' \
  --exclude='sawan/' \
  --exclude='docker/' \
  --exclude='.pytest_cache/' \
  --exclude='extra-non-related-web-resources-for-reference/' \
  --exclude='article_history.db' \
  --exclude='identity/' \
  --exclude='devices/' \
  --exclude='credentials/' \
  --exclude='projects/' \
  --exclude='telegram/' \
  "${SRC}/" "${DEST}/"

# Ensure pipeline state dir exists in image (data volume mounts over it)
mkdir -p "${DEST}/workspace-orchestrator/state"
mkdir -p "${DEST}/logs" "${DEST}/data"

# Patch all /home/bhard paths → container paths
find "$DEST" -type f \( \
  -name '*.json' -o -name '*.sh' -o -name '*.md' -o -name '*.py' -o -name '*.service' \
\) -not -path '*/node_modules/*' \
  -exec grep -l '/home/bhard' {} + 2>/dev/null \
  | while read -r f; do
      sed -i \
        -e "s|/home/bhard/.openclaw|${CONTAINER_ROOT}|g" \
        -e 's|/home/bhard/.npm-global/bin|/usr/local/bin|g' \
        -e 's|/home/bhard/.openclaw/logs|/home/bhard/.openclaw/logs|g' \
        "$f" || true
    done

# Fix chromium, gateway bind, and Bifrost URL in openclaw.json
BIFROST_URL="http://bifrost:8080/v1"
if [[ -f "${DEST}/openclaw.json" ]]; then
  sed -i 's|"executablePath": "[^"]*"|"executablePath": "/usr/bin/chromium"|' "${DEST}/openclaw.json"
  sed -i 's|"bind": "loopback"|"bind": "0.0.0.0"|' "${DEST}/openclaw.json"
  python3 - "${DEST}/openclaw.json" "$BIFROST_URL" <<'PY'
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
fi

# Patch legacy host-specific Bifrost URLs in scripts/docs
find "$DEST" -type f \( -name '*.json' -o -name '*.sh' -o -name '*.md' \) \
  -not -path '*/node_modules/*' \
  -exec grep -lE '192\.168\.32\.1:8888|host\.docker\.internal:8888|172\.30\.176\.1:8888' {} + 2>/dev/null \
  | while read -r f; do
      sed -i \
        -e "s|http://192\.168\.32\.1:8888/v1|${BIFROST_URL}|g" \
        -e "s|http://host\.docker\.internal:8888/v1|${BIFROST_URL}|g" \
        -e "s|http://172\.30\.176\.1:8888/v1|${BIFROST_URL}|g" \
        "$f" || true
    done

# Copy gog binary for Dockerfile
if [[ -x /usr/local/bin/gog ]]; then
  cp /usr/local/bin/gog "${GOG_BIN}/gog"
  echo "Copied gog binary to ${GOG_BIN}/gog"
else
  echo "ERROR: /usr/local/bin/gog not found — required for Google Drive uploads"
  exit 1
fi

echo ""
echo "Build context ready at ${DEST}"
echo "Next: ./export-data.sh && docker compose build"
