#!/usr/bin/env bash
# =============================================================================
# generate.sh — Pollinations.ai image generation (Creator Agent / Pixel)
# =============================================================================
# Usage:
#   bash generate.sh "<IMAGE PROMPT>"
#
# Environment (optional):
#   POLLINATIONS_API_KEY   — Secret key (sk_...) from enter.pollinations.ai
#   POLLINATIONS_BASE_URL  — API base (default: https://gen.pollinations.ai)
#   IMAGE_MODEL            — Primary model (default: flux)
#   IMAGE_MODEL_FALLBACK   — Fallback model (default: zimage)
#   OUTPUT_PATH            — Save path (default: /tmp/crypto-feature.jpg)
#   PROJECT_CONFIG         — Project JSON path (for creator.logo_path)
#   PROJECT_SLUG           — Per-run slug for result/error file names
#   STAMP_LOGO=1|0         — composite brand logo after save (default: 1)
#
# Output:
#   Exit 0 → /tmp/<slug>-image-result.txt contains the file path
#            + ${OUTPUT}.watermarked marker written
#   Exit 1 → /tmp/<slug>-image-error.log contains the human-readable error reason
# =============================================================================

set -euo pipefail

PROMPT="${1:-}"
POLLINATIONS_BASE_URL="${POLLINATIONS_BASE_URL:-https://gen.pollinations.ai}"
IMAGE_MODEL="${IMAGE_MODEL:-flux}"
IMAGE_MODEL_FALLBACK="${IMAGE_MODEL_FALLBACK:-zimage}"
STAMP_LOGO="${STAMP_LOGO:-1}"
OUTPUT_PATH="${OUTPUT_PATH:-/tmp/crypto-feature.jpg}"
_slug="${PROJECT_SLUG:-crypto}"
RESULT_FILE="/tmp/${_slug}-image-result.txt"
ERROR_FILE="/tmp/${_slug}-image-error.log"
LOGO_PATH="$HOME/.openclaw/assets/logo.png"
PROJECT_CFG="${PROJECT_CONFIG:-}"
if [ -n "$PROJECT_CFG" ] && [ -f "$PROJECT_CFG" ]; then
  _logo_rel=$(python3 "$HOME/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py" \
    --path "$PROJECT_CFG" --field creator.logo_path 2>/dev/null || true)
  if [ -n "$_logo_rel" ]; then
    LOGO_PATH="$HOME/.openclaw/$_logo_rel"
  fi
fi
MAX_GENERATE_RETRIES=3
WIDTH=1024
HEIGHT=576
MIN_BYTES=40960
NEGATIVE_SUFFIX=", no text, no watermark, no logo, no words, no letters, no signage, photorealistic editorial photography, not illustration, not cartoon, not 3d render"

log_error() { echo "[ERROR] $*" | tee -a "$ERROR_FILE"; }
log_warn() { echo "[WARNING] $*" | tee -a "$ERROR_FILE"; }
log_info() { echo "[INFO]  $*"; }
fatal() { log_error "$*"; exit 1; }

rm -f "$ERROR_FILE" "$RESULT_FILE"
[ -z "$PROMPT" ] && fatal "No prompt provided. Usage: bash generate.sh \"<prompt>\""
[ -z "${POLLINATIONS_API_KEY:-}" ] && fatal "POLLINATIONS_API_KEY is not set."
[ ${#PROMPT} -gt 1000 ] && fatal "Prompt too long (${#PROMPT} chars). Max 1000 characters."

FULL_PROMPT="${PROMPT}${NEGATIVE_SUFFIX}"

EFFECTIVE_OUTPUT="$OUTPUT_PATH"
[ -L "$OUTPUT_PATH" ] && EFFECTIVE_OUTPUT=$(readlink -f "$OUTPUT_PATH")
mkdir -p "$(dirname "$EFFECTIVE_OUTPUT")" 2>/dev/null || true

cleanup_failed_output() {
  if [ "${SUCCESS:-0}" -eq 1 ]; then
    return 0
  fi
  local size
  size=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo 0)
  if [ ! -s "$EFFECTIVE_OUTPUT" ] || [ "$size" -eq 0 ]; then
    rm -f "$EFFECTIVE_OUTPUT" "${EFFECTIVE_OUTPUT}.watermarked"
  fi
}
trap cleanup_failed_output EXIT

log_info "Starting image generation for prompt: ${PROMPT:0:80}..."
log_info "Primary: $IMAGE_MODEL | Fallback: $IMAGE_MODEL_FALLBACK | Size: ${WIDTH}x${HEIGHT} | Stamp logo: $STAMP_LOGO"

call_pollinations() {
  local model="$1"
  local timeout_sec="$2"
  local out_file="$3"

  GENERATION_MODEL="$model" \
  GENERATION_PROMPT="$FULL_PROMPT" \
  GENERATION_WIDTH="$WIDTH" \
  GENERATION_HEIGHT="$HEIGHT" \
  POLLINATIONS_BASE_URL="${POLLINATIONS_BASE_URL%/}" \
  OUT_FILE="$out_file" \
  TIMEOUT_SEC="$timeout_sec" \
  python3 - <<'PY'
import json
import os
import sys
import urllib.parse
import urllib.request

api_key = os.environ.get("POLLINATIONS_API_KEY", "")
if not api_key:
    print("FATAL: POLLINATIONS_API_KEY not set", file=sys.stderr)
    sys.exit(3)

base = os.environ["POLLINATIONS_BASE_URL"].rstrip("/")
model = os.environ["GENERATION_MODEL"]
prompt = os.environ["GENERATION_PROMPT"]
width = os.environ["GENERATION_WIDTH"]
height = os.environ["GENERATION_HEIGHT"]
out_file = os.environ["OUT_FILE"]
timeout = int(os.environ["TIMEOUT_SEC"])

encoded_prompt = urllib.parse.quote(prompt, safe="")
query = urllib.parse.urlencode({
    "model": model,
    "width": width,
    "height": height,
})
url = f"{base}/image/{encoded_prompt}?{query}"

req = urllib.request.Request(
    url,
    headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "openclaw-generate/1.0",
    },
    method="GET",
)

try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        http_code = resp.getcode()
        content_type = resp.headers.get("Content-Type", "")
        img_bytes = resp.read()
except urllib.error.HTTPError as e:
    http_code = e.code
    content_type = e.headers.get("Content-Type", "")
    img_bytes = e.read()
except Exception as e:
    print(f"TRANSIENT: request failed: {e}", file=sys.stderr)
    sys.exit(2)

def parse_error_message(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return raw[:300].decode("utf-8", errors="replace")
    err = data.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("code") or str(err)
    return str(err or data)

is_json = (
    content_type.startswith("application/json")
    or (img_bytes[:1] == b"{" and b'"error"' in img_bytes[:800])
)
if is_json:
    msg = parse_error_message(img_bytes)
    if http_code in (401, 402, 403):
        print(f"FATAL: HTTP {http_code}: {msg}", file=sys.stderr)
        sys.exit(3)
    if http_code == 400 and any(k in msg.lower() for k in ("safety", "blocked", "policy")):
        print(f"FATAL: HTTP {http_code}: {msg}", file=sys.stderr)
        sys.exit(3)
    print(f"TRANSIENT: HTTP {http_code}: {msg}", file=sys.stderr)
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
  local marker="${EFFECTIVE_OUTPUT}.watermarked"
  rm -f "$marker"

  if [ "$STAMP_LOGO" != "1" ]; then
    log_info "Logo stamping disabled (STAMP_LOGO=$STAMP_LOGO)."
    touch "$marker"
    return 0
  fi
  if ! command -v convert &>/dev/null; then
    fatal "WATERMARK: ImageMagick convert not found — cannot stamp logo."
  fi
  if [ ! -f "$LOGO_PATH" ]; then
    fatal "WATERMARK: logo not found at $LOGO_PATH (set creator.logo_path in project config)."
  fi
  log_info "Stamping logo from $LOGO_PATH..."
  TEMP_LOGO="/tmp/temp_logo_$$.png"
  if convert "$LOGO_PATH" -resize 100x "$TEMP_LOGO" 2>/dev/null && \
     composite -gravity SouthEast -geometry +16+16 "$TEMP_LOGO" "$EFFECTIVE_OUTPUT" "$EFFECTIVE_OUTPUT" 2>/dev/null; then
    log_info "Logo stamped."
    touch "$marker"
  else
    rm -f "$TEMP_LOGO"
    fatal "WATERMARK: logo composite failed for $EFFECTIVE_OUTPUT"
  fi
  rm -f "$TEMP_LOGO"
}

TEMP_RAW="/tmp/pollinations-raw-$$.bin"
SUCCESS=0
RETRY_DELAY=5

for attempt in $(seq 1 $MAX_GENERATE_RETRIES); do
  log_info "Generation round $attempt of $MAX_GENERATE_RETRIES..."

  for model_spec in "90:$IMAGE_MODEL" "120:$IMAGE_MODEL_FALLBACK"; do
    timeout_sec="${model_spec%%:*}"
    model="${model_spec#*:}"
    rm -f "$TEMP_RAW"
    log_info "Trying model=$model (timeout=${timeout_sec}s)..."

    set +e
    result=$(call_pollinations "$model" "$timeout_sec" "$TEMP_RAW" 2>&1)
    rc=$?
    set -e

    if [ "$rc" -eq 0 ]; then
      log_info "Model $model succeeded ($result)."
      ensure_jpeg "$TEMP_RAW" "$EFFECTIVE_OUTPUT"
      rm -f "$TEMP_RAW"

      FILE_SIZE=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo "0")
      if [ "$FILE_SIZE" -lt "$MIN_BYTES" ]; then
        log_error "Image too small (${FILE_SIZE} bytes, min $MIN_BYTES)."
        rm -f "$EFFECTIVE_OUTPUT" "${EFFECTIVE_OUTPUT}.watermarked"
        continue
      fi

      stamp_logo
      FINAL_SIZE=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo "0")
      log_info "Final image at $EFFECTIVE_OUTPUT — ${FINAL_SIZE} bytes (model=$model)"
      echo "$EFFECTIVE_OUTPUT" > "$RESULT_FILE"
      cp -f "$RESULT_FILE" /tmp/image-result.txt 2>/dev/null || true
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
[ "$SUCCESS" -eq 1 ] || fatal "Failed to generate image after $MAX_GENERATE_RETRIES rounds (primary + fallback each)."
exit 0
