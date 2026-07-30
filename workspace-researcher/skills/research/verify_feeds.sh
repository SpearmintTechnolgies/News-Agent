#!/usr/bin/env bash
# =============================================================================
# verify_feeds.sh — Smoke-test RSS feed URLs for the active project
# =============================================================================
# Usage:
#   bash verify_feeds.sh [--project <slug>]
#
# Reads research.rss_feeds from projects/<slug>.json via emit_feed_fetch_commands.
# Exit 0 if all feeds return HTTP 200 with RSS items; exit 1 if any fail.
# =============================================================================
set -uo pipefail

TIMEOUT=12
UA="Mozilla/5.0 (compatible; OpenClawScout/1.0)"
FAILED=0
PASSED=0
PROJECT_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ARG="$2"; shift 2 ;;
    *) shift ;;
  esac
done

EMIT_PY="$HOME/.openclaw/workspace-orchestrator/skills/pipeline/emit_feed_fetch_commands.py"
OUTDIR=$(mktemp -d)
trap 'rm -rf "$OUTDIR"' EXIT

ARGS=()
if [[ -n "$PROJECT_ARG" ]]; then
  ARGS+=(--project "$PROJECT_ARG")
fi

if ! python3 "$EMIT_PY" "${ARGS[@]}" --output-dir "$OUTDIR" > "$OUTDIR/fetch.sh" 2>"$OUTDIR/emit.log"; then
  echo "FAIL  could not emit feed commands"
  cat "$OUTDIR/emit.log"
  exit 1
fi

grep -E '^# project=' "$OUTDIR/fetch.sh" || true
echo "Scout RSS feed verification"
echo "=========================="

bash "$OUTDIR/fetch.sh" 2>/dev/null || true

has_rss_items() {
  local file="$1"
  grep -qiE '<(item|entry)(\s|>|/)' "$file" 2>/dev/null && return 0
  return 1
}

for xml in "$OUTDIR"/*.xml; do
  [[ -f "$xml" ]] || continue
  base=$(basename "$xml" .xml)
  src_file="$OUTDIR/${base}.source"
  name="Unknown"
  if [[ -f "$src_file" ]]; then
    name=$(cat "$src_file")
  fi
  if [[ ! -s "$xml" ]]; then
    echo "FAIL  $name — empty response"
    FAILED=$((FAILED + 1))
    continue
  fi
  if has_rss_items "$xml"; then
    count=$(grep -oiE '<(item|entry)(\s|>|/)' "$xml" 2>/dev/null | wc -l)
    echo "OK    $name — ~$count items"
    PASSED=$((PASSED + 1))
  else
    echo "FAIL  $name — no RSS items"
    FAILED=$((FAILED + 1))
  fi
done

echo "=========================="
echo "Passed: $PASSED  Failed: $FAILED"

if [[ "$FAILED" -gt 0 ]]; then
  exit 1
fi
echo "All feeds OK"
exit 0
