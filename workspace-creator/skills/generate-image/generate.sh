#!/usr/bin/env bash
# =============================================================================
# generate.sh — Vertex Imagen 4 via Bifrost (Creator Agent / Pixel)
# =============================================================================
# Usage:
#   bash generate.sh "<IMAGE PROMPT>"
#
# Environment (optional):
#   BIFROST_BASE_URL   — Bifrost OpenAI-compatible base (default: http://192.168.32.1:8888/v1)
#   IMAGE_MODEL            — Primary model (default: vertex/imagen-4.0-fast-generate-001)
#   IMAGE_MODEL_FALLBACK   — Quality fallback (default: vertex/imagen-4.0-generate-001)
#   STAMP_LOGO=1|0     — composite brand logo after save (default: 1)
#
# Output:
#   Exit 0 → /tmp/image-result.txt contains the file path
#   Exit 1 → /tmp/image-error.log contains the human-readable error reason
# =============================================================================

set -euo pipefail

PROMPT="${1:-}"
BIFROST_BASE_URL="${BIFROST_BASE_URL:-http://192.168.32.1:8888/v1}"
IMAGE_MODEL="${IMAGE_MODEL:-vertex/imagen-4.0-fast-generate-001}"
IMAGE_MODEL_FALLBACK="${IMAGE_MODEL_FALLBACK:-${IMAGE_MODEL_FAST:-vertex/imagen-4.0-generate-001}}"
STAMP_LOGO="${STAMP_LOGO:-1}"
OUTPUT_PATH="/tmp/crypto-feature.jpg"
RESULT_FILE="/tmp/image-result.txt"
ERROR_FILE="/tmp/image-error.log"
LOGO_PATH="$HOME/.openclaw/assets/logo.png"
MAX_GENERATE_RETRIES=3
WIDTH=1024
HEIGHT=576
MIN_BYTES=51200
NEGATIVE_SUFFIX=", no text, no watermark, no logo, no words, no letters, no signage, photorealistic editorial photography, not illustration, not cartoon, not 3d render"

log_error() { echo "[ERROR] $*" | tee -a "$ERROR_FILE"; }
log_warn() { echo "[WARNING] $*" | tee -a "$ERROR_FILE"; }
log_info() { echo "[INFO]  $*"; }
fatal() { log_error "$*"; exit 1; }

# Only one Imagen request at a time (prevents overlapping runs when agent restarts mid-job).
LOCK_FILE="/tmp/imagen-generate.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  RUNNING=$(pgrep -af 'generate-image/generate.sh' 2>/dev/null | head -3 || echo "unknown")
  echo "[ERROR] IMAGE_BUSY: another image generation is already running. Poll the existing process; do NOT start a second generate.sh. Active: ${RUNNING}" | tee "$ERROR_FILE"
  exit 1
fi

rm -f "$ERROR_FILE" "$RESULT_FILE"
[ -z "$PROMPT" ] && fatal "No prompt provided. Usage: bash generate.sh \"<prompt>\""
[ ${#PROMPT} -gt 1000 ] && fatal "Prompt too long (${#PROMPT} chars). Max 1000 characters."

FULL_PROMPT="${PROMPT}${NEGATIVE_SUFFIX}"

EFFECTIVE_OUTPUT="$OUTPUT_PATH"
[ -L "$OUTPUT_PATH" ] && EFFECTIVE_OUTPUT=$(readlink -f "$OUTPUT_PATH")
mkdir -p "$(dirname "$EFFECTIVE_OUTPUT")" 2>/dev/null || true

log_info "Starting image generation for prompt: ${PROMPT:0:80}..."
log_info "Primary: $IMAGE_MODEL | Fallback: $IMAGE_MODEL_FALLBACK | Size: ${WIDTH}x${HEIGHT} | Stamp logo: $STAMP_LOGO"
echo "[INFO] endpoint=${BIFROST_BASE_URL%/}/images/generations primary=$IMAGE_MODEL fallback=$IMAGE_MODEL_FALLBACK" >> "$ERROR_FILE"

call_imagen() {
  local model="$1"
  local timeout_sec="$2"
  local out_file="$3"

  GENERATION_MODEL="$model" \
  GENERATION_PROMPT="$FULL_PROMPT" \
  GENERATION_SIZE="${WIDTH}x${HEIGHT}" \
  BIFROST_URL="${BIFROST_BASE_URL%/}/images/generations" \
  OUT_FILE="$out_file" \
  TIMEOUT_SEC="$timeout_sec" \
  python3 - <<'PY'
import base64
import json
import os
import sys
import urllib.request

model = os.environ["GENERATION_MODEL"]
prompt = os.environ["GENERATION_PROMPT"]
size = os.environ["GENERATION_SIZE"]
url = os.environ["BIFROST_URL"]
out_file = os.environ["OUT_FILE"]
timeout = int(os.environ["TIMEOUT_SEC"])

body = json.dumps({
    "model": model,
    "prompt": prompt,
    "size": size,
    "n": 1,
}).encode("utf-8")

req = urllib.request.Request(
    url,
    data=body,
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer dummy",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        http_code = resp.getcode()
        raw = resp.read().decode("utf-8", errors="replace")
except urllib.error.HTTPError as e:
    http_code = e.code
    raw = e.read().decode("utf-8", errors="replace")
except Exception as e:
    print(f"TRANSIENT: request failed: {e}", file=sys.stderr)
    sys.exit(2)

try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print(f"TRANSIENT: invalid JSON (HTTP {http_code}): {raw[:300]}", file=sys.stderr)
    sys.exit(2)

err = data.get("error")
if err:
    if isinstance(err, dict):
        msg = err.get("message") or err.get("error") or str(err)
    else:
        msg = str(err)
    if http_code == 400 and any(k in msg.lower() for k in ("policy", "quota", "permission", "blocked", "safety")):
        print(f"FATAL: HTTP {http_code}: {msg}", file=sys.stderr)
        sys.exit(3)
    print(f"TRANSIENT: HTTP {http_code}: {msg}", file=sys.stderr)
    sys.exit(2)

items = data.get("data") or []
if not items:
    print(f"TRANSIENT: HTTP {http_code} but empty data: {raw[:300]}", file=sys.stderr)
    sys.exit(2)

item = items[0]
b64 = item.get("b64_json") or item.get("b64") or ""
image_url = item.get("url") or ""

if b64:
    img_bytes = base64.b64decode(b64)
elif image_url:
    try:
        with urllib.request.urlopen(image_url, timeout=60) as dl:
            img_bytes = dl.read()
    except Exception as e:
        print(f"TRANSIENT: download failed: {e}", file=sys.stderr)
        sys.exit(2)
else:
    print(f"TRANSIENT: no b64_json or url in response", file=sys.stderr)
    sys.exit(2)

if len(img_bytes) < 1024:
    print(f"TRANSIENT: image too small ({len(img_bytes)} bytes)", file=sys.stderr)
    sys.exit(2)

with open(out_file, "wb") as f:
    f.write(img_bytes)

print(f"OK:{len(img_bytes)}")
PY
}

ensure_jpeg() {
  local src="$1"
  local dst="$2"
  if file -b "$src" 2>/dev/null | grep -qiE 'JPEG|jpg'; then
    cp -f "$src" "$dst"
    return 0
  fi
  if command -v convert &>/dev/null; then
    if convert "$src" -quality 92 "$dst" 2>/dev/null; then
      log_info "Converted non-JPEG response to JPEG."
      return 0
    fi
  fi
  cp -f "$src" "$dst"
  log_warn "Saved image without JPEG conversion (format: $(file -b "$src" 2>/dev/null || echo unknown))."
}

stamp_logo() {
  if [ "$STAMP_LOGO" != "1" ]; then
    log_info "Logo stamping disabled (STAMP_LOGO=$STAMP_LOGO)."
    return 0
  fi
  if ! command -v convert &>/dev/null || [ ! -f "$LOGO_PATH" ]; then
    log_warn "Logo stamping skipped (convert or logo missing)."
    return 0
  fi
  log_info "Stamping logo..."
  TEMP_LOGO="/tmp/temp_logo_$$.png"
  if convert "$LOGO_PATH" -resize 100x "$TEMP_LOGO" 2>/dev/null && \
     composite -gravity SouthEast -geometry +16+16 "$TEMP_LOGO" "$EFFECTIVE_OUTPUT" "$EFFECTIVE_OUTPUT" 2>/dev/null; then
    log_info "Logo stamped."
  else
    log_warn "Logo stamping failed — continuing without watermark."
  fi
  rm -f "$TEMP_LOGO"
}

TEMP_RAW="/tmp/imagen-raw-$$.bin"
SUCCESS=0
RETRY_DELAY=5

for attempt in $(seq 1 $MAX_GENERATE_RETRIES); do
  log_info "Generation round $attempt of $MAX_GENERATE_RETRIES..."

  for model_spec in "180:$IMAGE_MODEL" "240:$IMAGE_MODEL_FALLBACK"; do
    timeout_sec="${model_spec%%:*}"
    model="${model_spec#*:}"
    rm -f "$TEMP_RAW"
    log_info "Trying model=$model (timeout=${timeout_sec}s)..."

    set +e
    result=$(call_imagen "$model" "$timeout_sec" "$TEMP_RAW" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -eq 0 ]; then
      log_info "Model $model succeeded ($result)."
      ensure_jpeg "$TEMP_RAW" "$EFFECTIVE_OUTPUT"
      rm -f "$TEMP_RAW"

      FILE_SIZE=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo "0")
      if [ "$FILE_SIZE" -lt "$MIN_BYTES" ]; then
        log_error "Image too small (${FILE_SIZE} bytes, min $MIN_BYTES)."
        rm -f "$EFFECTIVE_OUTPUT"
        continue
      fi

      stamp_logo
      FINAL_SIZE=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo "0")
      log_info "Final image at $EFFECTIVE_OUTPUT — ${FINAL_SIZE} bytes (model=$model)"
      echo "$EFFECTIVE_OUTPUT" > "$RESULT_FILE"
      echo "SUCCESS: $EFFECTIVE_OUTPUT"
      SUCCESS=1
      break 2
    elif [ "$rc" -eq 3 ]; then
      fatal "$result"
    else
      log_warn "Model $model failed: $result"
    fi
  done

  if [ "$attempt" -lt "$MAX_GENERATE_RETRIES" ]; then
    log_info "Waiting ${RETRY_DELAY}s before retry..."
    sleep "$RETRY_DELAY"
    RETRY_DELAY=$((RETRY_DELAY * 2))
  fi
done

rm -f "$TEMP_RAW"
[ "$SUCCESS" -eq 1 ] || fatal "Failed to generate image after $MAX_GENERATE_RETRIES rounds (primary + fast fallback each)."
exit 0
