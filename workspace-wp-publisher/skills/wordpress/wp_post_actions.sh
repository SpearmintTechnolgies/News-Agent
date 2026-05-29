#!/usr/bin/env bash
# wp_post_actions.sh — Update existing WordPress posts (draft / publish / content)
#
# Usage:
#   bash wp_post_actions.sh --post-id 123 --set-status draft
#   bash wp_post_actions.sh --post-id 123 --set-status publish
#   bash wp_post_actions.sh --post-id 123 --set-status publish --author 17
#   bash wp_post_actions.sh --post-id 123 --markdown /path/to/article.md --update-content
#
# Output (stdout):
#   WP_DRAFT_OK: post_id=... url=...
#   WP_PUBLISH_OK: post_id=... url=... [author_id=...]
#   WP_UPDATE_OK: post_id=...
#   WP_ACTION_FAILED: reason
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WP_URL="https://coinography.com"
WP_API="${WP_URL}/wp-json/wp/v2"
WP_USER="renu@coinography.com"
WP_PASS="PXjy ZopD 4q7z VDqq E1EC 5Dox"

POST_ID=""
SET_STATUS=""
AUTHOR_ID=""
MARKDOWN_PATH=""
UPDATE_CONTENT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --post-id) POST_ID="$2"; shift 2 ;;
    --set-status) SET_STATUS="$2"; shift 2 ;;
    --author) AUTHOR_ID="$2"; shift 2 ;;
    --markdown) MARKDOWN_PATH="$2"; shift 2 ;;
    --update-content) UPDATE_CONTENT=1; shift ;;
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

  if command -v pandoc &>/dev/null; then
    timeout 20s pandoc "$CLEAN_MD" -t html -o "$HTML_PATH" 2>/dev/null || cp "$CLEAN_MD" "$HTML_PATH"
  else
    cp "$CLEAN_MD" "$HTML_PATH"
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
