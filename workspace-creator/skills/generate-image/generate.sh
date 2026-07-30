#!/usr/bin/env bash
# =============================================================================
# generate.sh — Bifrost image generation (Creator Agent / Pixel)
# =============================================================================
# Usage:
#   bash generate.sh "<IMAGE PROMPT>"
#
# Environment (optional):
#   BIFROST_BASE_URL       — OpenAI-compatible API base (default from openclaw.json env)
#   IMAGE_MODEL            — Primary model (default: vertex/gemini-3.1-flash-lite-image)
#   IMAGE_MODEL_FALLBACK   — Fallback model (default: huggingface/.../FLUX.1-schnell)
#   OUTPUT_PATH            — Save path (default: /tmp/crypto-feature.jpg)
#   PROJECT_CONFIG         — Project JSON path (for creator.logo_path)
#   PROJECT_SLUG           — Per-run slug for result/error file names
#   STAMP_LOGO=1|0         — composite brand logo after save (default: 1)
#
# Output:
#   Exit 0 → /tmp/<slug>-image-result.txt contains the file path
#            + ${OUTPUT}.watermarked marker written
#            + publish/image-cost.json when OUTPUT_PATH is under a run dir
#   Exit 1 → /tmp/<slug>-image-error.log contains the human-readable error reason
# =============================================================================

set -euo pipefail

PROMPT="${1:-}"
IMAGE_MODEL="${IMAGE_MODEL:-pollinations/flux-realism}"
IMAGE_MODEL_FALLBACK1="${IMAGE_MODEL_FALLBACK1:-pollinations/flux}"
IMAGE_MODEL_FALLBACK2="${IMAGE_MODEL_FALLBACK2:-pollinations/turbo}"
IMAGE_MODEL_FALLBACK3="${IMAGE_MODEL_FALLBACK3:-pollinations/any-dark}"
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
WIDTH=1920
HEIGHT=1080
MIN_BYTES=40960
NEGATIVE_SUFFIX=", no text, no watermark, no logo, no words, no letters, no signage, 8k resolution, ultra detailed, sharp focus, cinematic studio lighting, photorealistic editorial photography, not illustration, not cartoon, not 3d render"

log_error() { echo "[ERROR] $*" | tee -a "$ERROR_FILE"; }
log_warn() { echo "[WARNING] $*" | tee -a "$ERROR_FILE"; }
log_info() { echo "[INFO]  $*"; }
fatal() { log_error "$*"; exit 1; }

rm -f "$ERROR_FILE" "$RESULT_FILE"
[ -z "$PROMPT" ] && fatal "No prompt provided. Usage: bash generate.sh \"<prompt>\""
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
log_info "Primary: $IMAGE_MODEL | Fallbacks: $IMAGE_MODEL_FALLBACK1, $IMAGE_MODEL_FALLBACK2, $IMAGE_MODEL_FALLBACK3 | Size: ${WIDTH}x${HEIGHT} | Stamp logo: $STAMP_LOGO"

call_pollinations() {
  local model="$1"
  local timeout_sec="$2"
  local out_file="$3"
  local api_key="${POLLINATIONS_API_KEY:-sk_E9wUldwB1d2LPkPfduyR1SVVZvzkGWaH}"

  GENERATION_MODEL="$model" \
  GENERATION_PROMPT="$FULL_PROMPT" \
  GENERATION_WIDTH="$WIDTH" \
  GENERATION_HEIGHT="$HEIGHT" \
  POLLINATIONS_KEY="$api_key" \
  OUT_FILE="$out_file" \
  TIMEOUT_SEC="$timeout_sec" \
  python3 - <<'PY'
import os
import sys
import urllib.parse
import urllib.request

model = os.environ.get("GENERATION_MODEL", "pollinations/flux-realism")
if "realism" in model:
    model_param = "flux-realism"
elif "turbo" in model:
    model_param = "turbo"
elif "dark" in model or "any-dark" in model:
    model_param = "any-dark"
else:
    model_param = "flux"

prompt = os.environ.get("GENERATION_PROMPT", "")
width = os.environ.get("GENERATION_WIDTH", "1920")
height = os.environ.get("GENERATION_HEIGHT", "1080")
api_key = os.environ.get("POLLINATIONS_KEY", "")
out_file = os.environ.get("OUT_FILE", "")
timeout = int(os.environ.get("TIMEOUT_SEC", "60"))

encoded_prompt = urllib.parse.quote(prompt)
url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&model={model_param}&nologo=true&enhance=true"

headers = {
    "Authorization": f"Bearer {api_key}",
    "User-Agent": "OpenClawCreator/1.0"
}

req = urllib.request.Request(url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status == 200:
            img_bytes = resp.read()
            if len(img_bytes) < 4096:
                print(f"TRANSIENT: image too small ({len(img_bytes)} bytes)", file=sys.stderr)
                sys.exit(2)
            with open(out_file, "wb") as f:
                f.write(img_bytes)
            print(f"OK:{len(img_bytes)}")
            sys.exit(0)
        else:
            print(f"TRANSIENT: HTTP {resp.status}", file=sys.stderr)
            sys.exit(2)
except Exception as e:
    print(f"TRANSIENT: pollinations error: {e}", file=sys.stderr)
    sys.exit(2)
PY
}

ensure_jpeg() {
  local src="$1"
  local dst="$2"
  # Copy pristine original raw image directly without resizing or lossy re-encoding
  cp -f "$src" "$dst"
  log_info "Preserved raw original image directly from Pollinations (size: $(stat -c%s "$dst" 2>/dev/null || echo 0) bytes)."
  return 0
}

write_image_cost() {
  local model="$1"
  local publish_dir
  publish_dir=$(dirname "$EFFECTIVE_OUTPUT")
  if [ "$(basename "$publish_dir")" = "media" ]; then
    publish_dir="$(dirname "$publish_dir")/publish"
  fi
  if [ ! -d "$publish_dir" ]; then
    mkdir -p "$publish_dir" 2>/dev/null || true
  fi

  local cost_file="$publish_dir/image-cost.json"

  IMAGE_COST_MODEL="$model" IMAGE_COST_FILE="$cost_file" python3 - <<'PY'
import json, os
m = os.environ.get("IMAGE_COST_MODEL", "pollinations/flux")
cf = os.environ.get("IMAGE_COST_FILE", "")
if not cf:
    sys.exit(0)
pricing_path = os.path.expanduser("~/.openclaw/workspace-orchestrator/config/image-model-pricing.json")
name = "Pollinations.ai Flux (Free)"
usd = 0.0
if os.path.isfile(pricing_path):
    try:
        with open(pricing_path, encoding="utf-8") as f:
            cfg = json.load(f)
        info = cfg.get(m, {})
        if info:
            name = info.get("name", name)
            usd = float(info.get("cost_usd_per_image", 0.0))
    except Exception:
        pass
data = {"model": m, "name": name, "cost_usd": usd}
try:
    with open(cf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
except Exception:
    pass
PY
}

stamp_logo() {
  if [ "$STAMP_LOGO" -ne 1 ]; then
    log_info "Logo stamping disabled (STAMP_LOGO=$STAMP_LOGO)."
    return 0
  fi

  if [ ! -f "$LOGO_PATH" ]; then
    log_warn "Logo not found at $LOGO_PATH; skipping brand stamp."
    return 0
  fi

  if ! command -v composite &>/dev/null || ! command -v convert &>/dev/null; then
    log_warn "ImageMagick (composite/convert) not found; skipping brand stamp."
    return 0
  fi

  log_info "Stamping logo from $LOGO_PATH..."
  local tmp_logo="/tmp/scaled-logo-$$.png"
  local tmp_stamped="/tmp/stamped-$$.jpg"

  convert "$LOGO_PATH" -resize 180x180 "$tmp_logo" 2>/dev/null || {
    log_warn "Failed to scale logo; skipping brand stamp."
    rm -f "$tmp_logo"
    return 0
  }

  composite -gravity SouthEast -geometry +40+40 "$tmp_logo" "$EFFECTIVE_OUTPUT" "$tmp_stamped" 2>/dev/null || {
    log_warn "Composite failed; keeping clean image."
    rm -f "$tmp_logo" "$tmp_stamped"
    return 0
  }

  mv -f "$tmp_stamped" "$EFFECTIVE_OUTPUT"
  touch "${EFFECTIVE_OUTPUT}.watermarked"
  rm -f "$tmp_logo"
  log_info "Logo stamped successfully."
  return 0
}

is_jpeg_file() {
  local path="$1"
  local magic
  magic=$(head -c 2 "$path" 2>/dev/null | od -An -tx1 | tr -d ' \n')
  [ "$magic" = "ffd8" ]
}

validate_final_image() {
  local path="$1"
  local size magic_hex

  if [ ! -f "$path" ]; then
    log_error "Final image missing: $path"
    return 1
  fi

  if ! is_jpeg_file "$path"; then
    magic_hex=$(head -c 3 "$path" 2>/dev/null | od -An -tx1 | tr -d ' \n')
    log_error "Final image is not a JPEG (magic bytes: ${magic_hex:-unknown})"
    return 1
  fi

  size=$(stat -c%s "$path" 2>/dev/null || echo "0")
  if [ "$size" -ge "$MIN_BYTES" ]; then
    log_info "Post-stamp validation OK (${size} bytes)."
    return 0
  fi

  log_warn "Post-stamp below min (${size} bytes, min ${MIN_BYTES}); attempting quality bump..."
  if command -v convert &>/dev/null; then
    if convert "$path" -quality 92 "$path" 2>/dev/null; then
      size=$(stat -c%s "$path" 2>/dev/null || echo "0")
      if [ "$size" -ge "$MIN_BYTES" ]; then
        log_info "Post-stamp validation OK after quality bump (${size} bytes)."
        return 0
      fi
    fi
  fi

  log_error "Post-stamp below min (${size} bytes, min ${MIN_BYTES}); quality bump failed."
  return 1
}

TEMP_RAW="/tmp/pollinations-raw-$$.bin"
SUCCESS=0
RETRY_DELAY=5

for attempt in $(seq 1 $MAX_GENERATE_RETRIES); do
  log_info "Generation round $attempt of $MAX_GENERATE_RETRIES..."

  for model_spec in "60:$IMAGE_MODEL" "60:$IMAGE_MODEL_FALLBACK1" "60:$IMAGE_MODEL_FALLBACK2" "60:$IMAGE_MODEL_FALLBACK3"; do
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

      stamp_logo
      if ! validate_final_image "$EFFECTIVE_OUTPUT"; then
        rm -f "$EFFECTIVE_OUTPUT" "${EFFECTIVE_OUTPUT}.watermarked"
        continue
      fi

      FINAL_SIZE=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo "0")
      log_info "Final image at $EFFECTIVE_OUTPUT — ${FINAL_SIZE} bytes (model=$model)"
      write_image_cost "$model"
      echo "$EFFECTIVE_OUTPUT" > "$RESULT_FILE"
      cp -f "$RESULT_FILE" /tmp/image-result.txt 2>/dev/null || true
      echo "SUCCESS: $EFFECTIVE_OUTPUT"
      SUCCESS=1
      break 2
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
