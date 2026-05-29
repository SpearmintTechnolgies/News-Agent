#!/usr/bin/env bash
# =============================================================================
# generate.sh — Leonardo AI Image Generation Skill for Pixel (Creator Agent)
# =============================================================================
# Usage:
#   bash generate.sh "<IMAGE PROMPT>"
#
# Environment (optional):
#   LEONARDO_API_KEY   — Leonardo API bearer token
#   STAMP_LOGO=1|0     — composite brand logo after download (default: 1)
#   USE_PHOTOREAL=1|0  — PhotoReal v2 stack (default: 1; falls back on 422)
#
# Output:
#   Exit 0 → /tmp/image-result.txt contains the file path
#   Exit 1 → /tmp/image-error.log contains the human-readable error reason
# =============================================================================

set -euo pipefail

# --- Config ------------------------------------------------------------------
PROMPT="${1:-}"
API_KEY="${LEONARDO_API_KEY:-dddd08ff-d8c3-4fec-98d9-9e8c060f4619}"
# Leonardo Vision XL — PhotoReal v2 compatible (editorial / stock photo)
MODEL_ID="${LEONARDO_MODEL_ID:-5c232a9e-9061-4777-980a-ddc8e65647c6}"
PRESET_STYLE="${LEONARDO_PRESET_STYLE:-STOCK_PHOTO}"
USE_PHOTOREAL="${USE_PHOTOREAL:-1}"
STAMP_LOGO="${STAMP_LOGO:-1}"
API_BASE="https://cloud.leonardo.ai/api/rest/v1"
OUTPUT_PATH="/tmp/crypto-feature.jpg"
RESULT_FILE="/tmp/image-result.txt"
ERROR_FILE="/tmp/image-error.log"
LOGO_PATH="$HOME/.openclaw/assets/logo.png"
MAX_GENERATE_RETRIES=3
POLL_ATTEMPTS=6
POLL_INTERVAL=10
WIDTH=1024
HEIGHT=576

NEGATIVE_PROMPT="deformed, distorted, disfigured, bad anatomy, bad hands, missing fingers, extra fingers, fused fingers, malformed limbs, crossed eyes, asymmetric eyes, blurry face, cartoon, illustration, 3d render, cgi, painting, anime, text, watermark, logo, words, letters, signage, readable text, keyboard keys"

log_error() { echo "[ERROR] $*" | tee -a "$ERROR_FILE"; }
log_warn() { echo "[WARNING] $*"; }
log_info() { echo "[INFO]  $*"; }
fatal() { log_error "$*"; exit 1; }

build_request_json() {
  local use_photoreal="$1"
  GENERATION_PROMPT="$PROMPT" \
  GENERATION_MODEL_ID="$MODEL_ID" \
  GENERATION_PRESET="$PRESET_STYLE" \
  GENERATION_NEGATIVE="$NEGATIVE_PROMPT" \
  GENERATION_WIDTH="$WIDTH" \
  GENERATION_HEIGHT="$HEIGHT" \
  GENERATION_USE_PHOTOREAL="$use_photoreal" \
  python3 - <<'PY'
import json, os
body = {
    "prompt": os.environ["GENERATION_PROMPT"],
    "modelId": os.environ["GENERATION_MODEL_ID"],
    "num_images": 1,
    "width": int(os.environ["GENERATION_WIDTH"]),
    "height": int(os.environ["GENERATION_HEIGHT"]),
    "alchemy": True,
    "enhancePrompt": False,
    "negative_prompt": os.environ["GENERATION_NEGATIVE"],
}
if os.environ.get("GENERATION_USE_PHOTOREAL") == "1":
    body["photoReal"] = True
    body["photoRealVersion"] = "v2"
    body["presetStyle"] = os.environ["GENERATION_PRESET"]
print(json.dumps(body))
PY
}

submit_generation() {
  local use_photoreal="$1"
  local request_json
  request_json="$(build_request_json "$use_photoreal")"
  HTTP_RESPONSE=$(curl --silent --write-out "\n__HTTP_STATUS__%{http_code}" \
    --request POST --url "$API_BASE/generations" \
    --header "accept: application/json" \
    --header "authorization: Bearer $API_KEY" \
    --header "content-type: application/json" \
    --data "$request_json" --max-time 60)
  HTTP_BODY=$(echo "$HTTP_RESPONSE" | sed '$d')
  HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tail -1 | sed 's/__HTTP_STATUS__//')
}

rm -f "$ERROR_FILE" "$RESULT_FILE"
[ -z "$PROMPT" ] && fatal "No prompt provided. Usage: bash generate.sh \"<prompt>\""
[ ${#PROMPT} -gt 1000 ] && fatal "Prompt too long (${#PROMPT} chars). Max 1000 characters."

log_info "Starting image generation for prompt: ${PROMPT:0:80}..."
log_info "Model: $MODEL_ID | PhotoReal: $USE_PHOTOREAL | Preset: $PRESET_STYLE | Size: ${WIDTH}x${HEIGHT} | Stamp logo: $STAMP_LOGO"

GENERATION_ID=""
RETRY_DELAY=5
PHOTOREAL_ACTIVE="$USE_PHOTOREAL"

for attempt in $(seq 1 $MAX_GENERATE_RETRIES); do
  log_info "Generation attempt $attempt of $MAX_GENERATE_RETRIES (photoReal=$PHOTOREAL_ACTIVE)..."
  submit_generation "$PHOTOREAL_ACTIVE"
  log_info "API response status: $HTTP_STATUS"

  [ "$HTTP_STATUS" = "401" ] && fatal "Authentication failed (401). API key is invalid or expired. Not retrying."
  [ "$HTTP_STATUS" = "403" ] && fatal "Access forbidden (403). Account may be suspended or quota exceeded. Not retrying."

  if [ "$HTTP_STATUS" = "422" ] && [ "$PHOTOREAL_ACTIVE" = "1" ]; then
    log_warn "PhotoReal request rejected (422). Retrying without PhotoReal (alchemy + CINEMATIC)..."
    PHOTOREAL_ACTIVE=0
    PRESET_STYLE="CINEMATIC"
    MODEL_ID="1e60896f-3c26-4296-8ecc-53e2afecc132"
    continue
  fi
  [ "$HTTP_STATUS" = "422" ] && fatal "Invalid request (422). Body: $HTTP_BODY. Not retrying."

  if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "201" ]; then
    GENERATION_ID=$(echo "$HTTP_BODY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('sdGenerationJob',{}).get('generationId',''))" 2>/dev/null || echo "")
    if [ -n "$GENERATION_ID" ]; then
      log_info "Generation submitted. ID: $GENERATION_ID (photoReal=$PHOTOREAL_ACTIVE)"
      break
    fi
    log_error "Got HTTP 200 but no generationId. Body: ${HTTP_BODY:0:200}"
  fi
  log_error "Transient error (status=$HTTP_STATUS). Waiting ${RETRY_DELAY}s..."
  sleep $RETRY_DELAY
  RETRY_DELAY=$((RETRY_DELAY * 2))
done

[ -z "$GENERATION_ID" ] && fatal "Failed to submit generation after $MAX_GENERATE_RETRIES attempts."

log_info "Polling (max $((POLL_ATTEMPTS * POLL_INTERVAL))s)..."
IMAGE_URL=""
for poll in $(seq 1 $POLL_ATTEMPTS); do
  sleep $POLL_INTERVAL
  log_info "Poll $poll of $POLL_ATTEMPTS..."
  POLL_RESULT=$(curl --silent --request GET --url "$API_BASE/generations/$GENERATION_ID" \
    --header "authorization: Bearer $API_KEY" --max-time 15)
  IMAGE_URL=$(echo "$POLL_RESULT" | python3 -c "
import json, sys
data = json.load(sys.stdin)
gen = data.get('generations_by_pk', {})
if gen.get('status') == 'COMPLETE' and gen.get('generated_images'):
    print(gen['generated_images'][0].get('url', '').strip())
" 2>/dev/null || echo "")
  if [ -n "$IMAGE_URL" ]; then
    log_info "Image ready! URL: $IMAGE_URL"
    break
  fi
  GEN_STATUS=$(echo "$POLL_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('generations_by_pk',{}).get('status','unknown'))" 2>/dev/null || echo 'unknown')
  log_info "Status: $GEN_STATUS"
done

[ -z "$IMAGE_URL" ] && fatal "Timed out waiting for image. GenerationId: $GENERATION_ID"

EFFECTIVE_OUTPUT="$OUTPUT_PATH"
[ -L "$OUTPUT_PATH" ] && EFFECTIVE_OUTPUT=$(readlink -f "$OUTPUT_PATH")
mkdir -p "$(dirname "$EFFECTIVE_OUTPUT")" 2>/dev/null || true

TEMP_DOWNLOAD="/tmp/leonardo-dl-${GENERATION_ID}.jpg"
log_info "Downloading to $EFFECTIVE_OUTPUT (via $TEMP_DOWNLOAD)..."

HTTP_DL_STATUS=$(curl --silent --location --write-out "%{http_code}" --output "$TEMP_DOWNLOAD" --max-time 60 \
  -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
  -H "Accept: image/webp,image/apng,image/*,*/*;q=0.8" \
  -H "Referer: https://app.leonardo.ai/" "$IMAGE_URL")

[ "$HTTP_DL_STATUS" != "200" ] && rm -f "$TEMP_DOWNLOAD" && fatal "Download failed HTTP $HTTP_DL_STATUS"

FILE_SIZE=$(stat -c%s "$TEMP_DOWNLOAD" 2>/dev/null || echo "0")
[ "$FILE_SIZE" -lt 51200 ] && rm -f "$TEMP_DOWNLOAD" && fatal "Downloaded file too small (${FILE_SIZE} bytes)"

cp -f "$TEMP_DOWNLOAD" "$EFFECTIVE_OUTPUT"
rm -f "$TEMP_DOWNLOAD"
log_info "Downloaded ${FILE_SIZE} bytes."

if [ "$STAMP_LOGO" = "1" ] && command -v convert &>/dev/null && [ -f "$LOGO_PATH" ]; then
  log_info "Stamping logo..."
  TEMP_LOGO="/tmp/temp_logo_$$.png"
  if convert "$LOGO_PATH" -resize 100x "$TEMP_LOGO" 2>/dev/null && \
     composite -gravity SouthEast -geometry +16+16 "$TEMP_LOGO" "$EFFECTIVE_OUTPUT" "$EFFECTIVE_OUTPUT" 2>/dev/null; then
    log_info "Logo stamped."
  else
    log_warn "Logo stamping failed — continuing without watermark."
  fi
  rm -f "$TEMP_LOGO"
elif [ "$STAMP_LOGO" != "1" ]; then
  log_info "Logo stamping disabled (STAMP_LOGO=$STAMP_LOGO)."
fi

FINAL_SIZE=$(stat -c%s "$EFFECTIVE_OUTPUT" 2>/dev/null || echo "0")
log_info "Final image at $EFFECTIVE_OUTPUT — ${FINAL_SIZE} bytes"
echo "$EFFECTIVE_OUTPUT" > "$RESULT_FILE"
echo "SUCCESS: $EFFECTIVE_OUTPUT"
exit 0
