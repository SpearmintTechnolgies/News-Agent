#!/usr/bin/env bash
# =============================================================================
# cleanup_run_artifacts.sh — Remove /tmp symlinks and active-run pointer
#                            Called ONLY on pipeline terminal state.
# =============================================================================
# Usage:
#   bash cleanup_run_artifacts.sh --manifest /path/to/manifest.json
#   bash cleanup_run_artifacts.sh --manifest /path/to/manifest.json --archive
#
# Options:
#   --manifest PATH   Path to manifest.json (required)
#   --archive         Keep RUN_DIR intact; only remove /tmp symlinks (default)
#                     Without --archive same behaviour (RUN_DIR never deleted here)
#
# IMPORTANT: RUN_DIR itself is never deleted. It is kept for 7 days as an audit
# trail and pruned automatically by init_run.sh on the next run.
# =============================================================================
set -euo pipefail

MANIFEST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --manifest) MANIFEST="$2"; shift 2 ;;
        --archive)  shift ;;  # accepted for compatibility; no-op (always archive)
        *) echo "[cleanup] Unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$MANIFEST" ]]; then
    echo "[cleanup] --manifest is required" >&2
    exit 1
fi

if [[ ! -f "$MANIFEST" ]]; then
    echo "[cleanup] Manifest not found: $MANIFEST — skipping cleanup" >&2
    exit 0
fi

# --------------------------------------------------------------------------
# Remove legacy /tmp symlinks so they don't point to a completed run
# --------------------------------------------------------------------------
LEGACY_PATHS=(
    "/tmp/research.json"
    "/tmp/researcher-raw.txt"
    "/tmp/crypto-article-raw.md"
    "/tmp/crypto-article.md"
    "/tmp/crypto-with-image.md"
    "/tmp/crypto-article.docx"
    "/tmp/crypto-feature.jpg"
    "/tmp/chart.png"
    "/tmp/pipeline-manifest.json"
    "/tmp/wp-result.json"
    "/tmp/openclaw_active_url.txt"
)

for path in "${LEGACY_PATHS[@]}"; do
    if [ -L "$path" ]; then
        rm -f "$path"
        echo "[cleanup] Removed symlink: $path"
    fi
done

# Remove active-run pointer
if [ -f "/tmp/crypto-active-run" ]; then
    rm -f /tmp/crypto-active-run
    echo "[cleanup] Removed /tmp/crypto-active-run"
fi

echo "[cleanup] Done. RUN_DIR preserved for audit: $(python3 -c "import json; m=json.load(open('${MANIFEST}')); print(m.get('run_dir','?'))" 2>/dev/null || echo '?')"
