#!/usr/bin/env bash
# wp_post_actions.sh -- Update existing WordPress posts (draft / publish / content)
#
# Per-site WP credentials come from the active project's config -- never
# hardcoded. The project is resolved from --project / $PROJECT_SLUG / manifest.
#
# Usage:
#   bash wp_post_actions.sh --post-id 123 --set-status draft
#   bash wp_post_actions.sh --post-id 123 --set-status publish --author 17
#   bash wp_post_actions.sh --post-id 123 --markdown /path/to/article.md --update-content
#   bash wp_post_actions.sh --post-id 123 --set-status draft --project coinnetwork
#   bash wp_post_actions.sh --post-id 123 --ensure-featured-image /path/to/feature.jpg
#
# Output (stdout):
#   WP_DRAFT_OK: post_id=... url=...
#   WP_PUBLISH_OK: post_id=... url=... [author_id=...]
#   WP_UPDATE_OK: post_id=...
#   WP_IMAGE_OK: already_set=<media_id>
#   WP_IMAGE_SET: media_id=<id>
#   WP_IMAGE_MISSING: <path>
#   WP_ACTION_FAILED: reason
set -euo pipefail

# Windows: python3 must be CPython, not the Microsoft Store stub (exit 49).
if [[ -x "/c/Users/Aditya Singh/AppData/Local/Programs/Python/Python311/python.exe" ]]; then
  python3() { "/c/Users/Aditya Singh/AppData/Local/Programs/Python/Python311/python.exe" "$@"; }
  export -f python3
  export PATH="/c/Users/Aditya Singh/AppData/Local/Programs/Python/Python311:/c/Program Files/Git/bin:/c/Program Files/Git/usr/bin:${PATH:-}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_CONFIG_PY="$HOME/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py"

POST_ID=""
SET_STATUS=""
AUTHOR_ID=""
MARKDOWN_PATH=""
UPDATE_CONTENT=0
ENSURE_IMAGE_PATH=""
REPLACE_IMAGE=0
PROJECT_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --post-id) POST_ID="$2"; shift 2 ;;
    --set-status) SET_STATUS="$2"; shift 2 ;;
    --author) AUTHOR_ID="$2"; shift 2 ;;
    --markdown) MARKDOWN_PATH="$2"; shift 2 ;;
    --update-content) UPDATE_CONTENT=1; shift ;;
    --ensure-featured-image) ENSURE_IMAGE_PATH="$2"; shift 2 ;;
    --replace-featured-image) ENSURE_IMAGE_PATH="$2"; REPLACE_IMAGE=1; shift 2 ;;
    --project) PROJECT_ARG="$2"; shift 2 ;;
    *) echo "WP_ACTION_FAILED: unknown arg $1"; exit 1 ;;
  esac
done

if [[ -z "$POST_ID" ]]; then
  echo "WP_ACTION_FAILED: --post-id required"
  exit 1
fi

fail() {
  echo "WP_ACTION_FAILED: $*"
  exit 1
}

cfg_field() {
  local field="$1"
  if [[ -n "$PROJECT_ARG" ]]; then
    python3 "$PROJECT_CONFIG_PY" --slug "$PROJECT_ARG" --field "$field"
  else
    python3 "$PROJECT_CONFIG_PY" --field "$field"
  fi
}

cfg_password() {
  if [[ -n "$PROJECT_ARG" ]]; then
    python3 "$PROJECT_CONFIG_PY" --slug "$PROJECT_ARG" --password
  else
    python3 "$PROJECT_CONFIG_PY" --password
  fi
}

PROJECT_SLUG_RESOLVED=$(cfg_field slug 2>/dev/null) || fail "cannot resolve project"
WP_URL=$(cfg_field wordpress.url 2>/dev/null)       || fail "missing wordpress.url for $PROJECT_SLUG_RESOLVED"
WP_USER=$(cfg_field wordpress.user 2>/dev/null)     || fail "missing wordpress.user for $PROJECT_SLUG_RESOLVED"
WP_PASS=$(cfg_password 2>/dev/null)                 || fail "missing WP app password for $PROJECT_SLUG_RESOLVED"
WP_API="${WP_URL}/wp-json/wp/v2"

clean_markdown() {
  local src="$1"
  local dst="$2"
  python3 - <<PYEOF
import re

with open("${src}", "r", encoding="utf-8") as f:
    raw = f.read()

meta_hdr = re.search(r"^META\s*$", raw, re.MULTILINE)
if meta_hdr:
    after = raw[meta_hdr.end() :].lstrip("\n")
    h1_m = re.search(r"^#\s", after, re.MULTILINE)
    dash_m = re.search(r"^---\s*$", after, re.MULTILINE)
    end = len(after)
    if h1_m:
        end = min(end, h1_m.start())
    if dash_m:
        end = min(end, dash_m.start())
    clean = raw[: meta_hdr.start()] + after[end:].lstrip("\n")
else:
    clean = raw

clean = re.sub(r"<thinking>.*?</thinking>", "", clean, flags=re.DOTALL | re.IGNORECASE)
sources_footer = re.compile(
    r"(?:\n---\s*)?(?:\n\*\*Sources:\*\*|\nSources:\s*\n|\n#{1,3}\s+Sources)\s*\n.*",
    re.DOTALL | re.IGNORECASE,
)
clean = sources_footer.sub("\n", clean)
clean = re.sub(r"^\[?Word Count:.*?\]?\s*$", "", clean, flags=re.MULTILINE | re.IGNORECASE)
clean = re.sub(r"^#{1,3}\s+Word\s+Count\s*$", "", clean, flags=re.MULTILINE | re.IGNORECASE)
clean = clean.replace("[CHART_PLACEHOLDER]", "")
clean = re.sub(r"!\[[^\]]*\]\(/tmp/chart\.png\)\s*\n?", "", clean)
clean = re.sub(r"\n{3,}", "\n\n", clean)
clean = clean.strip() + "\n"

with open("${dst}", "w", encoding="utf-8") as f:
    f.write(clean)
PYEOF
}

wp_post_json() {
  local payload="$1"
  curl --silent --write-out "\n__STATUS__%{http_code}" \
    --user "${WP_USER}:${WP_PASS}" \
    --request POST "${WP_API}/posts/${POST_ID}" \
    --header "Content-Type: application/json" \
    --data "$payload" \
    --max-time 60
}

wp_get_json() {
  local endpoint="$1"
  curl --silent --write-out "\n__STATUS__%{http_code}" \
    --user "${WP_USER}:${WP_PASS}" \
    "${WP_API}/${endpoint}" \
    --max-time 60
}

if [[ -n "$ENSURE_IMAGE_PATH" ]]; then
  FEAT_RESP=$(wp_get_json "posts/${POST_ID}?_fields=featured_media")
  FEAT_HTTP=$(echo "$FEAT_RESP" | tail -1 | sed 's/__STATUS__//')
  FEAT_BODY=$(echo "$FEAT_RESP" | sed '$d')
  if [[ "$FEAT_HTTP" != "200" ]]; then
    echo "WP_IMAGE_MISSING: GET featured_media HTTP ${FEAT_HTTP}"
    exit 0
  fi
  EXISTING_MEDIA=$(echo "$FEAT_BODY" | python3 -c \
    "import json,sys; print(int(json.load(sys.stdin).get('featured_media') or 0))" 2>/dev/null || echo 0)
  if [[ "$REPLACE_IMAGE" -eq 0 && "$EXISTING_MEDIA" -gt 0 ]]; then
    echo "WP_IMAGE_OK: already_set=${EXISTING_MEDIA}"
    exit 0
  fi

  EFFECTIVE_IMAGE="$ENSURE_IMAGE_PATH"
  if [[ -f "$ENSURE_IMAGE_PATH" ]]; then
    EFFECTIVE_IMAGE=$(readlink -f "$ENSURE_IMAGE_PATH" 2>/dev/null || echo "$ENSURE_IMAGE_PATH")
  fi

  if [[ ! -f "$EFFECTIVE_IMAGE" ]]; then
    echo "WP_IMAGE_MISSING: ${ENSURE_IMAGE_PATH}"
    exit 0
  fi

  FILE_SIZE=$(stat -c%s "$EFFECTIVE_IMAGE" 2>/dev/null || stat -f%z "$EFFECTIVE_IMAGE" 2>/dev/null || echo 0)
  if [[ "$FILE_SIZE" -le 40960 ]]; then
    echo "WP_IMAGE_MISSING: ${ENSURE_IMAGE_PATH} (too small: ${FILE_SIZE} bytes)"
    exit 0
  fi

  MEDIA_RESPONSE=$(curl --silent --write-out "\n__STATUS__%{http_code}" \
    --user "${WP_USER}:${WP_PASS}" \
    --request POST "${WP_API}/media" \
    --header "Content-Disposition: attachment; filename=${PROJECT_SLUG_RESOLVED}-feature.jpg" \
    --header "Content-Type: image/jpeg" \
    --data-binary @"$EFFECTIVE_IMAGE" \
    --max-time 60)

  MEDIA_BODY=$(echo "$MEDIA_RESPONSE" | sed '$d')
  MEDIA_STATUS=$(echo "$MEDIA_RESPONSE" | tail -1 | sed 's/__STATUS__//')

  if [[ "$MEDIA_STATUS" != "201" && "$MEDIA_STATUS" != "200" ]]; then
    echo "WP_IMAGE_MISSING: upload HTTP ${MEDIA_STATUS}"
    exit 0
  fi

  MEDIA_ID=$(echo "$MEDIA_BODY" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('id', 0))" 2>/dev/null || echo 0)
  if [[ "$MEDIA_ID" -le 0 ]]; then
    echo "WP_IMAGE_MISSING: upload returned no media id"
    exit 0
  fi

  PATCH_PAYLOAD=$(python3 -c "import json; print(json.dumps({'featured_media': int('${MEDIA_ID}')}))")
  PATCH_RESP=$(wp_post_json "$PATCH_PAYLOAD")
  PATCH_HTTP=$(echo "$PATCH_RESP" | tail -1 | sed 's/__STATUS__//')
  if [[ "$PATCH_HTTP" != "200" ]]; then
    echo "WP_IMAGE_MISSING: set featured_media HTTP ${PATCH_HTTP}"
    exit 0
  fi

  echo "WP_IMAGE_SET: media_id=${MEDIA_ID}"
  exit 0
fi

if [[ -n "$SET_STATUS" ]]; then
  case "$SET_STATUS" in
    publish|draft|pending|future) ;;
    *) fail "invalid status '$SET_STATUS'" ;;
  esac
  if [[ -n "$AUTHOR_ID" ]]; then
    PAYLOAD=$(python3 -c "import json; print(json.dumps({'status': '${SET_STATUS}', 'author': int('${AUTHOR_ID}')}))")
  else
    PAYLOAD=$(python3 -c "import json; print(json.dumps({'status': '${SET_STATUS}'}))")
  fi
  RESP=$(wp_post_json "$PAYLOAD")
  HTTP=$(echo "$RESP" | tail -1 | sed 's/__STATUS__//')
  BODY=$(echo "$RESP" | sed '$d')
  if [[ "$HTTP" != "200" ]]; then
    fail "set-status HTTP $HTTP: ${BODY:0:200}"
  fi
  URL=$(echo "$BODY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('link',''))" 2>/dev/null || echo "")
  if [[ "$SET_STATUS" == "draft" ]]; then
    echo "WP_DRAFT_OK: post_id=${POST_ID} url=${URL}"
  else
    if [[ -n "$AUTHOR_ID" ]]; then
      echo "WP_PUBLISH_OK: post_id=${POST_ID} url=${URL} author_id=${AUTHOR_ID}"
    else
      echo "WP_PUBLISH_OK: post_id=${POST_ID} url=${URL}"
    fi
  fi
  exit 0
fi

if [[ "$UPDATE_CONTENT" -eq 1 ]]; then
  [[ -f "$MARKDOWN_PATH" ]] || fail "markdown not found: $MARKDOWN_PATH"
  CLEAN_MD=$(mktemp /tmp/wp-action-clean-XXXX.md)
  HTML_PATH=$(mktemp /tmp/wp-action-html-XXXX.html)
  trap 'rm -f "$CLEAN_MD" "$HTML_PATH"' EXIT

  clean_markdown "$MARKDOWN_PATH" "$CLEAN_MD"

  if command -v pandoc &>/dev/null && pandoc -v >/dev/null 2>&1; then
    timeout 20s pandoc "$CLEAN_MD" -t html -o "$HTML_PATH" 2>/dev/null \
      || python3 "${SCRIPT_DIR}/md_to_html.py" "$CLEAN_MD" -o "$HTML_PATH" \
      || fail "markdown→html failed"
  else
    python3 "${SCRIPT_DIR}/md_to_html.py" "$CLEAN_MD" -o "$HTML_PATH" \
      || fail "markdown→html failed (no pandoc)"
  fi

  if [[ -f "${SCRIPT_DIR}/html_to_gutenberg.py" ]]; then
    python3 "${SCRIPT_DIR}/html_to_gutenberg.py" "$HTML_PATH" -o "$HTML_PATH" 2>/dev/null || true
  fi

  PAYLOAD=$(python3 - <<PYEOF
import json
with open("${HTML_PATH}", "r", encoding="utf-8") as f:
    content = f.read()
print(json.dumps({"content": content}))
PYEOF
)
  RESP=$(wp_post_json "$PAYLOAD")
  HTTP=$(echo "$RESP" | tail -1 | sed 's/__STATUS__//')
  BODY=$(echo "$RESP" | sed '$d')
  if [[ "$HTTP" != "200" ]]; then
    fail "update-content HTTP $HTTP: ${BODY:0:200}"
  fi
  echo "WP_UPDATE_OK: post_id=${POST_ID}"
  exit 0
fi

fail "specify --set-status or --markdown --update-content"
