#!/usr/bin/env bash
# =============================================================================
# publish.sh — WordPress Publishing Skill for Scribe (wp-publisher agent)
# =============================================================================
# Usage:
#   bash publish.sh [--title "Title"] [--excerpt "Excerpt"]
#                  [--article /path/to/article.md] [--image /path/to/image.jpg]
#                  [--status publish|draft|pending|future]
#
# Smart extraction: reads SEO Title, Meta Description, URL Slug directly from
# the Writer's META block in the article — no manual passing needed.
#
# Pipeline cleanup: strips META block, Sources footer, Word Count line, and thinking blocks
# before publishing so only clean article content reaches WordPress.
#
# Output:
#   Exit 0 → /tmp/wp-result.txt contains the WordPress post URL
#   Exit 1 → /tmp/wp-error.log  contains the human-readable error reason
# =============================================================================

set -euo pipefail

# --- Config ------------------------------------------------------------------
WP_URL="https://coinography.com"
WP_API="${WP_URL}/wp-json/wp/v2"
RM_API="${WP_URL}/wp-json/rankmath/v1"
WP_USER="renu@coinography.com"
WP_PASS="PXjy ZopD 4q7z VDqq E1EC 5Dox"
CATEGORY_ID=17
POST_STATUS="draft"

ARTICLE_PATH="/tmp/crypto-article.md"
IMAGE_PATH="/tmp/crypto-feature.jpg"
CLEAN_MD="/tmp/crypto-article-clean.md"
HTML_PATH="/tmp/crypto-article.html"
RESULT_FILE="/tmp/wp-result.txt"
ERROR_FILE="/tmp/wp-error.log"
MAX_RETRIES=3

# --- Argument Parsing --------------------------------------------------------
TITLE=""
EXCERPT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)   TITLE="$2";   shift 2 ;;
    --excerpt) EXCERPT="$2"; shift 2 ;;
    --article) ARTICLE_PATH="$2"; shift 2 ;;
    --image)   IMAGE_PATH="$2";   shift 2 ;;
    --status)
      POST_STATUS="$2"
      case "$POST_STATUS" in
        publish|draft|pending|future) ;;
        *) fatal "Invalid --status '$POST_STATUS' (use publish, draft, pending, or future)" ;;
      esac
      shift 2
      ;;
    *) shift ;;
  esac
done

# --- Helpers -----------------------------------------------------------------
log_info()  { echo "[INFO]  $*"; }
log_error() { echo "[ERROR] $*" | tee -a "$ERROR_FILE"; }
fatal()     { log_error "$*"; exit 1; }

# --- Setup -------------------------------------------------------------------
rm -f "$ERROR_FILE" "$RESULT_FILE" "$CLEAN_MD" "$HTML_PATH" /tmp/wp-meta.txt

if [ ! -f "$ARTICLE_PATH" ]; then
  fatal "Article not found at '$ARTICLE_PATH'. Has the Writer agent run yet?"
fi

# =============================================================================
# PHASE 0: Extract metadata from META block + produce clean article
# =============================================================================
log_info "Parsing article — extracting metadata and stripping pipeline internals..."

python3 - <<PYEOF
import re

with open("${ARTICLE_PATH}", "r", encoding="utf-8") as f:
    raw = f.read()

meta_title = ""
meta_desc  = ""
meta_slug  = ""
focus_kw   = ""
secondary_raw = ""
meta_found = False

def meta_line(block, label):
    m = re.search(
        rf"^\s*(?:[-*]\s*)?{re.escape(label)}:\s*(.+)$",
        block,
        re.MULTILINE,
    )
    return m.group(1).strip() if m else ""

meta_hdr = re.search(r"^META\s*$", raw, re.MULTILINE)
if meta_hdr:
    meta_found = True
    after = raw[meta_hdr.end() :].lstrip("\n")
    h1_m = re.search(r"^#\s", after, re.MULTILINE)
    dash_m = re.search(r"^---\s*$", after, re.MULTILINE)
    end = len(after)
    if h1_m:
        end = min(end, h1_m.start())
    if dash_m:
        end = min(end, dash_m.start())
    block = after[:end]
    meta_title = meta_line(block, "SEO Title")
    meta_desc = meta_line(block, "Meta Description")
    meta_slug = meta_line(block, "URL Slug").lower()
    focus_kw = meta_line(block, "Primary Keyword")
    secondary_raw = meta_line(block, "Secondary Keywords")
    clean = raw[: meta_hdr.start()] + after[end:].lstrip("\n")
else:
    clean = raw

clean = re.sub(r"<thinking>.*?</thinking>", "", clean, flags=re.DOTALL | re.IGNORECASE)

sources_stripped = False
_before = clean

sources_footer = re.compile(
    r"(?:\n---\s*)?(?:\n\*\*Sources:\*\*|\nSources:\s*\n|\n#{1,3}\s+Sources)\s*\n.*",
    re.DOTALL | re.IGNORECASE,
)
clean = sources_footer.sub("\n", clean)
if clean != _before:
    sources_stripped = True
else:
    _before = clean
    wc_heading = re.compile(
        r"\n#{1,3}\s+Word\s+Count\s*\n.*",
        re.DOTALL | re.IGNORECASE,
    )
    clean = wc_heading.sub("\n", clean)
    if clean != _before:
        sources_stripped = True

clean = re.sub(r"^\[?Word Count:.*?\]?\s*$", "", clean, flags=re.MULTILINE | re.IGNORECASE)
clean = re.sub(r"^#{1,3}\s+Word\s+Count\s*$", "", clean, flags=re.MULTILINE | re.IGNORECASE)
clean = re.sub(
    r"^(?:Approx\.?\s*)?[\d,]+\s+words\s*$",
    "",
    clean,
    flags=re.MULTILINE | re.IGNORECASE,
)
clean = clean.replace("[CHART_PLACEHOLDER]", "")
clean = re.sub(r"!\[[^\]]*\]\(/tmp/chart\.png\)\s*\n?", "", clean)
clean = re.sub(r"\n{3,}", "\n\n", clean)
clean = clean.strip() + "\n"

article_h1 = ""
h1_m = re.search(r"^#\s+(.+)$", clean, re.MULTILINE)
if h1_m:
    article_h1 = h1_m.group(1).strip()

with open("/tmp/wp-meta.txt", "w", encoding="utf-8") as mf:
    mf.write("META_FOUND=" + ("1" if meta_found else "0") + "\n")
    mf.write("META_TITLE=" + meta_title + "\n")
    mf.write("META_DESC=" + meta_desc + "\n")
    mf.write("META_SLUG=" + meta_slug + "\n")
    mf.write("FOCUS_KEYWORD=" + focus_kw + "\n")
    mf.write("SECONDARY_KEYWORDS=" + secondary_raw + "\n")
    mf.write("ARTICLE_H1=" + article_h1 + "\n")
    mf.write("SOURCES_STRIPPED=" + ("1" if sources_stripped else "0") + "\n")

with open("${CLEAN_MD}", "w", encoding="utf-8") as f:
    f.write(clean)

print("[INFO]  META block found:" if meta_found else "[INFO]  META block not found")
print(f"[INFO]    SEO Title:        {meta_title[:70]}")
print(f"[INFO]    Meta Description: {meta_desc[:80]}")
print(f"[INFO]    URL Slug:         {meta_slug}")
print(f"[INFO]    Focus Keyword:    {focus_kw[:60]}")
print(f"[INFO]    Secondary Keywords: {secondary_raw[:80]}")
print(f"[INFO]    Article H1:         {article_h1[:70]}")
if sources_stripped:
    print("[INFO]  Sources footer stripped from body")
print(f"[INFO]  Clean article written to ${CLEAN_MD}")
PYEOF

# --- Read extracted meta into bash -------------------------------------------
EXTRACTED_TITLE=""
EXTRACTED_DESC=""
EXTRACTED_SLUG=""
FOCUS_KEYWORD=""
SECONDARY_KEYWORDS=""
RANK_MATH_FOCUS_KEYWORD=""
ARTICLE_H1=""
SOURCES_STRIPPED="0"
META_FOUND="0"

if [ -f /tmp/wp-meta.txt ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      META_FOUND)   META_FOUND="$value" ;;
      META_TITLE)   EXTRACTED_TITLE="$value" ;;
      META_DESC)    EXTRACTED_DESC="$value"  ;;
      META_SLUG)    EXTRACTED_SLUG="$value"  ;;
      FOCUS_KEYWORD) FOCUS_KEYWORD="$value" ;;
      SECONDARY_KEYWORDS) SECONDARY_KEYWORDS="$value" ;;
      ARTICLE_H1)   ARTICLE_H1="$value" ;;
      SOURCES_STRIPPED) SOURCES_STRIPPED="$value" ;;
    esac
  done < /tmp/wp-meta.txt
  rm -f /tmp/wp-meta.txt
fi

# Build comma-separated Rank Math focus keyword string (primary first, then secondaries)
RANK_MATH_FOCUS_KEYWORD=$(python3 - <<PYEOF
primary = """${FOCUS_KEYWORD}"""
secondary = """${SECONDARY_KEYWORDS}"""

def split_keywords(raw):
    if not raw:
        return []
    parts = []
    for piece in raw.split(","):
        kw = piece.strip().strip('"').strip("'")
        if kw:
            parts.append(kw)
    return parts

seen = set()
ordered = []
for kw in ([primary] if primary else []) + split_keywords(secondary):
    key = kw.lower()
    if key not in seen:
        seen.add(key)
        ordered.append(kw)
print(", ".join(ordered))
PYEOF
)

# --- WordPress post title (CLI → article H1 → SEO title → date fallback) -----
# SEO Title (EXTRACTED_TITLE) is for Rank Math only, not the visible post headline.
POST_TITLE=""
if [ -n "$TITLE" ]; then
  POST_TITLE="$TITLE"
  log_info "Post title source: CLI --title"
elif [ -n "$ARTICLE_H1" ]; then
  POST_TITLE="$ARTICLE_H1"
  log_info "Post title source: article H1"
elif [ -n "$EXTRACTED_TITLE" ]; then
  POST_TITLE="$EXTRACTED_TITLE"
  log_info "Post title source: META SEO Title (no H1 found)"
else
  POST_TITLE="Crypto News - $(date +%Y-%m-%d)"
  log_info "Post title source: date fallback"
fi

# --- Resolve final excerpt (CLI → META description → first 3 body lines) -----
if [ -z "$EXCERPT" ]; then
  if [ -n "$EXTRACTED_DESC" ]; then
    EXCERPT="$EXTRACTED_DESC"
    log_info "Excerpt source: META block Meta Description"
  else
    EXCERPT=$(grep -v "^#" "$CLEAN_MD" 2>/dev/null | grep -v "^[[:space:]]*$" | \
              head -3 | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g' | cut -c1-300 || echo "")
    log_info "Excerpt source: first body paragraphs"
  fi
fi

SLUG="$EXTRACTED_SLUG"

log_info "Post title:    $POST_TITLE"
log_info "SEO title:     ${EXTRACTED_TITLE:-<none>} (Rank Math only)"
log_info "Final Slug:    ${SLUG:-<WordPress auto-generate>}"
log_info "Focus Keyword:    ${FOCUS_KEYWORD:-<none>}"
log_info "Secondary Keywords: ${SECONDARY_KEYWORDS:-<none>}"
log_info "Rank Math Keywords: ${RANK_MATH_FOCUS_KEYWORD:-<none>}"
log_info "Post Status:   $POST_STATUS | Category ID: $CATEGORY_ID"

# Alt text for uploaded media (feature + chart)
MEDIA_ALT_TEXT="$FOCUS_KEYWORD"
[ -z "$MEDIA_ALT_TEXT" ] && MEDIA_ALT_TEXT="$POST_TITLE"

patch_media_alt() {
  local mid="$1"
  local alt="$2"
  [ -z "$mid" ] || [ "$mid" = "0" ] || [ -z "$alt" ] && return 0
  local resp http payload
  payload=$(ALT_TEXT="$alt" python3 -c 'import json, os; print(json.dumps({"alt_text": os.environ["ALT_TEXT"]}))')
  resp=$(curl --silent --write-out "\n__STATUS__%{http_code}" \
    --user "${WP_USER}:${WP_PASS}" \
    --request POST "${WP_API}/media/${mid}" \
    --header "Content-Type: application/json" \
    --data "$payload" \
    --max-time 30)
  http=$(echo "$resp" | tail -1 | sed 's/__STATUS__//')
  if [ "$http" = "200" ]; then
    log_info "Media ID $mid alt_text set."
  else
    log_error "Media alt_text update failed for ID $mid (HTTP $http)."
  fi
}

# =============================================================================
# PHASE 1: Upload Featured Image
# =============================================================================
MEDIA_ID=0

EFFECTIVE_IMAGE="$IMAGE_PATH"
if [ -f "$IMAGE_PATH" ]; then
  EFFECTIVE_IMAGE=$(readlink -f "$IMAGE_PATH" 2>/dev/null || echo "$IMAGE_PATH")
fi

if [ -f "$EFFECTIVE_IMAGE" ]; then
  FILE_SIZE=$(stat -c%s "$EFFECTIVE_IMAGE" 2>/dev/null || stat -f%z "$EFFECTIVE_IMAGE" 2>/dev/null || echo 0)
  if [ "$FILE_SIZE" -gt 51200 ]; then
    log_info "Uploading featured image from $EFFECTIVE_IMAGE ($FILE_SIZE bytes)..."

    MEDIA_RESPONSE=$(curl --silent --write-out "\n__STATUS__%{http_code}" \
      --user "${WP_USER}:${WP_PASS}" \
      --request POST "${WP_API}/media" \
      --header "Content-Disposition: attachment; filename=crypto-feature.jpg" \
      --header "Content-Type: image/jpeg" \
      --data-binary @"$EFFECTIVE_IMAGE" \
      --max-time 60)

    MEDIA_BODY=$(echo "$MEDIA_RESPONSE" | sed '$d')
    MEDIA_STATUS=$(echo "$MEDIA_RESPONSE" | tail -1 | sed 's/__STATUS__//')
    log_info "Image upload: HTTP $MEDIA_STATUS"

    if [ "$MEDIA_STATUS" = "201" ] || [ "$MEDIA_STATUS" = "200" ]; then
      MEDIA_ID=$(echo "$MEDIA_BODY" | python3 -c \
        "import json,sys; print(json.load(sys.stdin).get('id', 0))" 2>/dev/null || echo 0)
      log_info "Image uploaded. Media ID: $MEDIA_ID"
      patch_media_alt "$MEDIA_ID" "$MEDIA_ALT_TEXT"
    elif [ "$MEDIA_STATUS" = "401" ] || [ "$MEDIA_STATUS" = "403" ]; then
      log_error "Image upload auth error (HTTP $MEDIA_STATUS). Proceeding without image."
    else
      log_error "Image upload failed (HTTP $MEDIA_STATUS). Proceeding without featured image."
    fi
  else
    log_error "Image file too small ($FILE_SIZE bytes). Skipping."
  fi
else
  log_info "No image at '$IMAGE_PATH'. Publishing without featured image."
fi

# =============================================================================
# PHASE 2: Convert Clean Markdown → HTML
# =============================================================================
log_info "Converting clean article to HTML..."

if command -v pandoc &>/dev/null; then
  if timeout 20s pandoc "$CLEAN_MD" -t html -o "$HTML_PATH" 2>/dev/null; then
    log_info "Pandoc conversion successful."
  else
    log_error "Pandoc failed or timed out. Using raw content fallback."
    cp "$CLEAN_MD" "$HTML_PATH"
  fi
else
  log_error "Pandoc not installed. Using raw markdown as content."
  cp "$CLEAN_MD" "$HTML_PATH"
fi

# Strip H1 from body only when it differs from post title (avoid wrong duplicate)
if [ -n "$POST_TITLE" ] && [ -f "$HTML_PATH" ]; then
  python3 - <<PYEOF
import re
from html import unescape

path = "${HTML_PATH}"
post_title = """${POST_TITLE}""".strip()

def norm_text(s):
    return re.sub(r"\s+", " ", s).strip()

with open(path, "r", encoding="utf-8") as f:
    html = f.read()

m = re.search(r"<h1[^>]*>(.*?)</h1>\s*", html, flags=re.DOTALL | re.IGNORECASE)
if not m:
    print("[INFO]  No H1 in HTML to evaluate.")
else:
    h1_text = re.sub(r"<[^>]+>", "", m.group(1))
    h1_text = norm_text(unescape(h1_text))
    if h1_text == norm_text(post_title):
        new_html = html[: m.start()] + html[m.end() :]
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_html)
        print("[INFO]  Removed duplicate H1 from body (same as post title; theme shows title above image).")
    else:
        print(f"[INFO]  Keeping body H1 (differs from post title): {h1_text[:60]!r}")
PYEOF
fi

# =============================================================================
# PHASE 2.5: Convert HTML → Gutenberg blocks (matches default editor paste)
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HTML_PATH" ] && [ -f "${SCRIPT_DIR}/html_to_gutenberg.py" ]; then
  log_info "Converting HTML to Gutenberg block format..."
  if python3 "${SCRIPT_DIR}/html_to_gutenberg.py" "$HTML_PATH" -o "$HTML_PATH"; then
    log_info "Gutenberg block conversion successful."
  else
    log_error "Gutenberg conversion failed. Wrapping raw HTML in wp:html block."
    python3 - <<PYEOF
path = "${HTML_PATH}"
with open(path, "r", encoding="utf-8") as f:
    html = f.read().strip()
wrapped = (
    '<!-- wp:group {"layout":{"type":"constrained","contentSize":"720px"}} -->\n'
    '<div class="wp-block-group coinography-article">\n'
    f'<!-- wp:html -->\n{html}\n<!-- /wp:html -->\n'
    '</div>\n<!-- /wp:group -->'
)
with open(path, "w", encoding="utf-8") as f:
    f.write(wrapped)
print("[INFO]  Fallback wp:html wrapper applied.")
PYEOF
  fi
else
  log_error "html_to_gutenberg.py not found; content may render as narrow raw HTML."
fi

# =============================================================================
# PHASE 3: Create WordPress Post (with retry + exponential backoff)
# =============================================================================
log_info "Creating WordPress post (status=$POST_STATUS, max $MAX_RETRIES attempts)..."

POST_URL=""
POST_ID=""
RETRY_DELAY=5

for attempt in $(seq 1 $MAX_RETRIES); do
  log_info "Post attempt $attempt of $MAX_RETRIES..."

  PAYLOAD=$(python3 - <<PYEOF
import json

title    = """${POST_TITLE}"""
excerpt  = """${EXCERPT}"""
slug     = """${SLUG}"""
media_id = int("${MEDIA_ID}") if "${MEDIA_ID}" else 0

with open("${HTML_PATH}", "r", encoding="utf-8") as f:
    content = f.read()

data = {
    "title":      title,
    "content":    content,
    "excerpt":    excerpt,
    "status":     "${POST_STATUS}",
    "categories": [${CATEGORY_ID}]
}

if slug:
    data["slug"] = slug

if media_id > 0:
    data["featured_media"] = media_id

print(json.dumps(data))
PYEOF
)

  POST_RESPONSE=$(curl --silent --write-out "\n__STATUS__%{http_code}" \
    --user "${WP_USER}:${WP_PASS}" \
    --request POST "${WP_API}/posts" \
    --header "Content-Type: application/json" \
    --data "$PAYLOAD" \
    --max-time 45)

  POST_BODY=$(echo "$POST_RESPONSE" | sed '$d')
  POST_HTTP=$(echo "$POST_RESPONSE" | tail -1 | sed 's/__STATUS__//')
  log_info "Response: HTTP $POST_HTTP"

  # Fatal errors — do not retry
  [ "$POST_HTTP" = "401" ] && fatal "Auth failed (401). Check username and Application Password. Not retrying."
  [ "$POST_HTTP" = "403" ] && fatal "Permission denied (403). User needs Author/Editor role. Not retrying."
  [ "$POST_HTTP" = "404" ] && fatal "API not found (404). REST API may be disabled at $WP_URL. Not retrying."
  [ "$POST_HTTP" = "422" ] && fatal "Invalid post data (422). Body: ${POST_BODY:0:300}. Not retrying."

  # Success
  if [ "$POST_HTTP" = "201" ]; then
    POST_URL=$(echo "$POST_BODY" | python3 -c \
      "import json,sys; print(json.load(sys.stdin).get('link',''))" 2>/dev/null || echo "")
    POST_ID=$(echo "$POST_BODY" | python3 -c \
      "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

    if [ -n "$POST_URL" ]; then
      log_info "Post created! ID: $POST_ID | URL: $POST_URL"
      break
    else
      log_error "HTTP 201 but no link in response. Body: ${POST_BODY:0:200}"
    fi
  fi

  # Transient — retry with backoff
  log_error "Transient error (HTTP $POST_HTTP). Retrying in ${RETRY_DELAY}s..."
  sleep $RETRY_DELAY
  RETRY_DELAY=$((RETRY_DELAY * 2))
done

# =============================================================================
# PHASE 4: Validate & Write Result
# =============================================================================
if [ -z "$POST_URL" ]; then
  fatal "Failed after $MAX_RETRIES attempts. Last HTTP: $POST_HTTP. Body: ${POST_BODY:0:300}"
fi

if ! echo "$POST_URL" | grep -q "^http"; then
  fatal "Invalid post URL returned: '$POST_URL'"
fi

# =============================================================================
# PHASE 3.5: Rank Math SEO (native REST API only)
# =============================================================================
RANK_MATH_APPLIED=false

if [ -z "$EXTRACTED_TITLE" ] && [ -z "$EXTRACTED_DESC" ] && [ -z "$RANK_MATH_FOCUS_KEYWORD" ]; then
  fatal "META block missing SEO Title, Meta Description, and keywords. Cannot set Rank Math fields."
fi

log_info "Updating Rank Math SEO via ${RM_API}/updateMeta (max $MAX_RETRIES attempts)..."

RM_RETRY_DELAY=5
RM_HTTP=""
RM_BODY=""

for attempt in $(seq 1 $MAX_RETRIES); do
  log_info "Rank Math attempt $attempt of $MAX_RETRIES..."

  RM_PAYLOAD=$(python3 - <<PYEOF
import json

meta = {}
title = """${EXTRACTED_TITLE}"""
desc = """${EXTRACTED_DESC}"""
keywords = """${RANK_MATH_FOCUS_KEYWORD}"""
slug = """${SLUG}"""

if title:
    meta["rank_math_title"] = title
    meta["rank_math_facebook_title"] = title
    meta["rank_math_twitter_title"] = title
if desc:
    meta["rank_math_description"] = desc
    meta["rank_math_facebook_description"] = desc
    meta["rank_math_twitter_description"] = desc
if keywords:
    meta["rank_math_focus_keyword"] = keywords
if slug:
    meta["permalink"] = slug

print(json.dumps({
    "objectID": int("${POST_ID}"),
    "objectType": "post",
    "meta": meta,
}))
PYEOF
)

  RM_RESPONSE=$(curl --silent --write-out "\n__STATUS__%{http_code}" \
    --user "${WP_USER}:${WP_PASS}" \
    --request POST "${RM_API}/updateMeta" \
    --header "Content-Type: application/json" \
    --data "$RM_PAYLOAD" \
    --max-time 45)

  RM_BODY=$(echo "$RM_RESPONSE" | sed '$d')
  RM_HTTP=$(echo "$RM_RESPONSE" | tail -1 | sed 's/__STATUS__//')
  log_info "Rank Math response: HTTP $RM_HTTP"

  [ "$RM_HTTP" = "401" ] && fatal "Rank Math auth failed (401). Check Application Password."
  [ "$RM_HTTP" = "403" ] && fatal "Rank Math permission denied (403)."
  [ "$RM_HTTP" = "404" ] && fatal "Rank Math API not found (404). Is Rank Math active?"

  if [ "$RM_HTTP" = "200" ]; then
    RANK_MATH_APPLIED=true
    log_info "Rank Math SEO fields updated."
    break
  fi

  log_error "Rank Math transient error (HTTP $RM_HTTP). Body: ${RM_BODY:0:200}"
  sleep "$RM_RETRY_DELAY"
  RM_RETRY_DELAY=$((RM_RETRY_DELAY * 2))
done

if [ "$RANK_MATH_APPLIED" != "true" ]; then
  fatal "Rank Math updateMeta failed after $MAX_RETRIES attempts. Last HTTP: $RM_HTTP. Body: ${RM_BODY:0:300}"
fi

echo "$POST_URL" > "$RESULT_FILE"

log_info "================================================"
log_info "SUCCESS — WordPress post saved!"
log_info "  Post ID:   $POST_ID"
log_info "  Post URL:  $POST_URL"
log_info "  Post title:  $POST_TITLE"
log_info "  SEO title:   ${EXTRACTED_TITLE:-<none>}"
log_info "  Slug:      ${SLUG:-<auto>}"
log_info "  Image ID:  $MEDIA_ID"
log_info "  Status:    $POST_STATUS"
log_info "  Rank Math SEO:     applied"
log_info "  Focus keyword:     ${FOCUS_KEYWORD:-<none>}"
log_info "  Rank Math keywords: ${RANK_MATH_FOCUS_KEYWORD:-<none>}"
log_info "================================================"
log_info "Clean article: $CLEAN_MD"
if [ "$POST_STATUS" = "draft" ]; then
  log_info "Review draft:  $POST_URL?preview=true"
else
  log_info "Live post:     $POST_URL"
fi

# Write structured JSON result for orchestrator
python3 - <<PYEOF
import json
result = {
  "status": "ok",
  "post_id": "${POST_ID}",
  "draft_url": "${POST_URL}",
  "post_status": "${POST_STATUS}",
  "feature_image_uploaded": ${MEDIA_ID} != 0,
  "meta_stripped": True,
  "sources_footer_stripped": "${SOURCES_STRIPPED}" == "1",
  "rank_math_applied": True,
  "content_format": "gutenberg_blocks",
  "article_headline": """${POST_TITLE}""",
  "seo_title": """${EXTRACTED_TITLE}""",
  "focus_keyword": """${FOCUS_KEYWORD}""",
  "secondary_keywords": """${SECONDARY_KEYWORDS}""",
  "rank_math_focus_keyword": """${RANK_MATH_FOCUS_KEYWORD}""",
}
with open("/tmp/wp-result.json", "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
PYEOF

# =============================================================================
# PHASE 5: Record History
# =============================================================================
if [ -f "/tmp/openclaw_active_url.txt" ]; then
  ACTIVE_URL=$(cat /tmp/openclaw_active_url.txt)
  log_info "Recording article history for URL: $ACTIVE_URL"
  bash ~/.openclaw/workspace-wp-publisher/skills/history/article_history.sh add "$ACTIVE_URL"
  rm -f "/tmp/openclaw_active_url.txt"
fi

echo "SUCCESS: $POST_URL"
exit 0
