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
# Output (run-scoped — safe for concurrent coinography + memecoinist runs):
#   Exit 0 → $RUN_DIR/publish/wp-url.txt + wordpress.json (canonical)
#            /tmp/${PROJECT_SLUG}-wp-result.txt + .json (per-slug)
#            /tmp/wp-result.txt (legacy best-effort only)
#   Exit 1 → /tmp/wp-error.log contains the human-readable error reason
# =============================================================================

set -euo pipefail

# --- Config (project-driven) -------------------------------------------------
# Per-site WordPress credentials + category come from the active project's
# config file, never from hardcoded values. Project resolution order:
#   1. --project flag (if passed)
#   2. $PROJECT_SLUG env
#   3. $PROJECT_CONFIG env (path to projects/<slug>.json)
#   4. $PIPELINE_MANIFEST's "project" field
#   5. fallback: "coinography"
#
# To override the project from the CLI, add `--project <slug>` to the
# argument list (parsed below).

SCRIPT_DIR_PUBLISH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_CONFIG_PY="$HOME/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py"

POST_STATUS="draft"
PROJECT_ARG=""
CLEAN_MD=""
HTML_PATH=""
ERROR_FILE="/tmp/wp-error.log"
MAX_RETRIES=3

ARTICLE_PATH=""
IMAGE_PATH=""
RUN_DIR=""
RESULT_FILE=""
RESULT_JSON=""
WP_JSON_PATH=""
WP_URL_PATH=""

# --- Helpers (defined early so arg parsing can fatal) ------------------------
log_info()  { echo "[INFO]  $*"; }
log_error() { echo "[ERROR] $*" | tee -a "$ERROR_FILE" >&2; }
fatal()     { log_error "$*"; exit 1; }

# --- Argument Parsing --------------------------------------------------------
TITLE=""
EXCERPT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)   TITLE="$2";   shift 2 ;;
    --excerpt) EXCERPT="$2"; shift 2 ;;
    --article) ARTICLE_PATH="$2"; shift 2 ;;
    --image)   IMAGE_PATH="$2";   shift 2 ;;
    --project) PROJECT_ARG="$2";  shift 2 ;;
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

# --- Resolve project -> credentials + paths ----------------------------------
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

PROJECT_SLUG_RESOLVED=$(cfg_field slug) || fatal "Cannot resolve project (have you initialized a run?)"
# Reliability gate: resolved project must match the manifest's project.
MANIFEST_PATH="${PIPELINE_MANIFEST:-/tmp/openclaw-active-manifest.json}"
if [[ -f "$MANIFEST_PATH" ]]; then
  MANIFEST_PROJECT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('project',''))" "$MANIFEST_PATH" 2>/dev/null || true)
  if [[ -n "$MANIFEST_PROJECT" && "$MANIFEST_PROJECT" != "$PROJECT_SLUG_RESOLVED" ]]; then
    fatal "PROJECT MISMATCH: resolved='$PROJECT_SLUG_RESOLVED' but manifest at $MANIFEST_PATH says project='$MANIFEST_PROJECT'. Aborting to prevent publishing to the wrong site."
  fi
fi
WP_URL=$(cfg_field wordpress.url)             || fatal "missing wordpress.url in project '$PROJECT_SLUG_RESOLVED'"
WP_USER=$(cfg_field wordpress.user)           || fatal "missing wordpress.user in project '$PROJECT_SLUG_RESOLVED'"
WP_PASS=$(cfg_password)                       || fatal "missing wordpress.app_password_ref or password file for '$PROJECT_SLUG_RESOLVED'"
FALLBACK_CATEGORY_ID=$(cfg_field wordpress.fallback_category_id 2>/dev/null || echo "")
# Back-compat: older configs used a single wordpress.category_id.
if [ -z "$FALLBACK_CATEGORY_ID" ]; then
  FALLBACK_CATEGORY_ID=$(cfg_field wordpress.category_id 2>/dev/null || echo "17")
fi
DEFAULT_STATUS=$(cfg_field wordpress.default_status 2>/dev/null || echo "draft")
# If the user didn't pass --status, fall back to the project's default.
if [[ "$POST_STATUS" == "draft" ]] && [[ "$DEFAULT_STATUS" != "draft" ]] && [[ "$DEFAULT_STATUS" != "" ]]; then
  POST_STATUS="$DEFAULT_STATUS"
fi

WP_API="${WP_URL}/wp-json/wp/v2"
RM_API="${WP_URL}/wp-json/rankmath/v1"

# Resolve RUN_DIR from manifest (canonical run bundle root).
if [[ -n "${PIPELINE_MANIFEST:-}" && -f "${PIPELINE_MANIFEST}" ]]; then
  RUN_DIR="$(dirname "$(readlink -f "$PIPELINE_MANIFEST" 2>/dev/null || echo "$PIPELINE_MANIFEST")")"
elif [[ -n "${CRYPTO_RUN_DIR:-}" && -d "${CRYPTO_RUN_DIR}" ]]; then
  RUN_DIR="$CRYPTO_RUN_DIR"
fi

# Run-scoped publish outputs (source of truth for build_and_send_card.py).
if [[ -n "$RUN_DIR" ]]; then
  mkdir -p "${RUN_DIR}/publish"
  WP_JSON_PATH="${RUN_DIR}/publish/wordpress.json"
  WP_URL_PATH="${RUN_DIR}/publish/wp-url.txt"
fi

# Per-slug /tmp paths (safe across concurrent runs; legacy /tmp/crypto-* still
# resolve via init_run.sh symlinks for coinography backward compat).
RESULT_FILE="/tmp/${PROJECT_SLUG_RESOLVED}-wp-result.txt"
RESULT_JSON="/tmp/${PROJECT_SLUG_RESOLVED}-wp-result.json"
ARTICLE_PATH="${ARTICLE_PATH:-/tmp/${PROJECT_SLUG_RESOLVED}-article.md}"
if [[ -z "${IMAGE_PATH:-}" ]]; then
  if [[ -n "$RUN_DIR" && -f "${RUN_DIR}/media/feature.jpg" ]]; then
    IMAGE_PATH="${RUN_DIR}/media/feature.jpg"
  else
    IMAGE_PATH="/tmp/${PROJECT_SLUG_RESOLVED}-feature.jpg"
  fi
fi
CLEAN_MD="/tmp/${PROJECT_SLUG_RESOLVED}-article-clean.md"
HTML_PATH="/tmp/${PROJECT_SLUG_RESOLVED}-article.html"

# --- Resolve WordPress category IDs ------------------------------------------
# Picker assigns 1 primary + up to 2 secondary categories, resolved to numeric
# IDs by validate_picks.py and carried through validated.json as
# wp_category_ids. Read them here; fall back to the project's
# fallback_category_id when absent (legacy runs, single-story path, or errors).
VALIDATED_JSON="${VALIDATED_JSON:-/tmp/${PROJECT_SLUG_RESOLVED}-research.json}"
[ -f "$VALIDATED_JSON" ] || VALIDATED_JSON="/tmp/research.json"
CATEGORY_IDS=$(VJSON="$VALIDATED_JSON" FB="$FALLBACK_CATEGORY_ID" python3 - <<'PY'
import json, os
vjson = os.environ.get("VJSON", "")
fb = os.environ.get("FB", "17").strip() or "17"
ids = []
try:
    with open(vjson, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("wp_category_ids")
    if isinstance(raw, list):
        for x in raw:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in ids:
                ids.append(n)
except Exception:
    ids = []
if not ids:
    try:
        ids = [int(fb)]
    except ValueError:
        ids = [17]
print(",".join(str(i) for i in ids))
PY
)
[ -n "$CATEGORY_IDS" ] || CATEGORY_IDS="$FALLBACK_CATEGORY_ID"

# --- Setup -------------------------------------------------------------------
rm -f "$ERROR_FILE" "$RESULT_FILE" "$CLEAN_MD" "$HTML_PATH" /tmp/wp-meta.txt

log_info "Project:    $PROJECT_SLUG_RESOLVED"
log_info "WP URL:     $WP_URL"
log_info "WP user:    $WP_USER"
log_info "Categories: $CATEGORY_IDS (fallback=$FALLBACK_CATEGORY_ID)"
log_info "Article:    $ARTICLE_PATH"
log_info "Image:      $IMAGE_PATH"

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
log_info "Post Status:   $POST_STATUS | Category IDs: $CATEGORY_IDS"

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
  WATERMARK_MARKER="${EFFECTIVE_IMAGE}.watermarked"
  if [ ! -f "$WATERMARK_MARKER" ]; then
    fatal "WATERMARK: feature image at $EFFECTIVE_IMAGE lacks .watermarked marker — refusing to upload a pre-stamp image. Re-run generate-image."
  fi
  FILE_SIZE=$(stat -c%s "$EFFECTIVE_IMAGE" 2>/dev/null || stat -f%z "$EFFECTIVE_IMAGE" 2>/dev/null || echo 0)
  if [ "$FILE_SIZE" -gt 51200 ]; then
    log_info "Uploading featured image from $EFFECTIVE_IMAGE ($FILE_SIZE bytes)..."

    MEDIA_RESPONSE=$(curl --silent --write-out "\n__STATUS__%{http_code}" \
      --user "${WP_USER}:${WP_PASS}" \
      --request POST "${WP_API}/media" \
      --header "Content-Disposition: attachment; filename=${PROJECT_SLUG_RESOLVED}-feature.jpg" \
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
    s = s.translate(str.maketrans({
        "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-",
    }))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

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
    f'<div class="wp-block-group ${PROJECT_SLUG_RESOLVED}-article">\n'
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

category_ids = [int(x) for x in "${CATEGORY_IDS}".split(",") if x.strip()]

data = {
    "title":      title,
    "content":    content,
    "excerpt":    excerpt,
    "status":     "${POST_STATUS}",
    "categories": category_ids
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

write_publish_results() {
  echo "$POST_URL" > "$RESULT_FILE"
  if [[ -n "$WP_URL_PATH" ]]; then
    echo "$POST_URL" > "$WP_URL_PATH"
  fi
  # Legacy global paths — best-effort compat only, not source of truth.
  echo "$POST_URL" > /tmp/wp-result.txt 2>/dev/null || true
}

write_publish_results

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

# Write structured JSON result for orchestrator (run-scoped + per-slug paths)
python3 - <<PYEOF
import json
import os

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

paths = [
    "${RESULT_JSON}",
    "${WP_JSON_PATH}",
    "/tmp/wp-result.json",  # legacy best-effort only
]
for path in paths:
    if not path:
        continue
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except OSError:
        pass

print(json.dumps(result, indent=2))
PYEOF

# =============================================================================
# PHASE 5: Record History
# =============================================================================
ACTIVE_URL=""
if [[ -n "$RUN_DIR" && -f "${RUN_DIR}/.active_url" ]]; then
  ACTIVE_URL=$(cat "${RUN_DIR}/.active_url")
elif [ -f "/tmp/${PROJECT_SLUG_RESOLVED}-active-url.txt" ]; then
  ACTIVE_URL=$(cat "/tmp/${PROJECT_SLUG_RESOLVED}-active-url.txt")
elif [ -f "/tmp/openclaw_active_url.txt" ]; then
  ACTIVE_URL=$(cat /tmp/openclaw_active_url.txt)
fi
if [ -n "$ACTIVE_URL" ]; then
  log_info "Recording article history for URL: $ACTIVE_URL"
  bash ~/.openclaw/workspace-wp-publisher/skills/history/article_history.sh add "$ACTIVE_URL"
  rm -f "${RUN_DIR}/.active_url" "/tmp/${PROJECT_SLUG_RESOLVED}-active-url.txt" "/tmp/openclaw_active_url.txt" 2>/dev/null || true
fi

echo "SUCCESS: $POST_URL"
exit 0
