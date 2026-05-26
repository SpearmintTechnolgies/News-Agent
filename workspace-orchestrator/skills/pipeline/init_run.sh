#!/usr/bin/env bash
# =============================================================================
# init_run.sh — Initialize one isolated run-bundle for a pipeline execution
# =============================================================================
# Usage (from orchestrator Step 0 bash block):
#
#   bash ~/.openclaw/workspace-orchestrator/skills/pipeline/init_run.sh
#   source /tmp/crypto-run-env.sh
#
# After sourcing /tmp/crypto-run-env.sh the shell has:
#   $RUN_ID          — e.g. 20260521-120000
#   $RUN_DIR         — /tmp/crypto-run-20260521-120000
#   $CRYPTO_RUN_DIR  — same as RUN_DIR
#   $PIPELINE_MANIFEST — $RUN_DIR/manifest.json
#
# The run-bundle layout:
#   $RUN_DIR/
#     manifest.json
#     .run_started          (epoch stamp)
#     research/
#       raw.json            (Scout output)
#       validated.json      (Step 1 validator output)
#     article/
#       raw.md              (Quill output)
#       final.md            (sync + sanitize output)
#       with-image.md       (Step 4 image embed)
#       article.docx        (pandoc output)
#     media/
#       feature.jpg
#       chart.png
#     publish/
#       google-drive.json
#       wordpress.json
#
# All legacy /tmp/... handoff paths become symlinks into this bundle.
# Any old real files at those paths are removed first.
# =============================================================================

RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="/tmp/crypto-run-${RUN_ID}"

# --------------------------------------------------------------------------
# 1. Create nested directory tree + empty placeholder files
# --------------------------------------------------------------------------
mkdir -p \
  "${RUN_DIR}/research" \
  "${RUN_DIR}/article" \
  "${RUN_DIR}/media" \
  "${RUN_DIR}/publish"

# Empty placeholders prevent "file not found" errors before agents write
touch \
  "${RUN_DIR}/research/raw.json" \
  "${RUN_DIR}/research/validated.json" \
  "${RUN_DIR}/article/raw.md" \
  "${RUN_DIR}/article/final.md" \
  "${RUN_DIR}/article/with-image.md" \
  "${RUN_DIR}/media/feature.jpg" \
  "${RUN_DIR}/media/chart.png"

# Epoch stamp for freshness checks
date +%s > "${RUN_DIR}/.run_started"

# --------------------------------------------------------------------------
# 2. Write manifest.json (routing table) — atomic write via .tmp
# --------------------------------------------------------------------------
python3 - <<PYEOF
import json, datetime, os

run_id  = "${RUN_ID}"
run_dir = "${RUN_DIR}"

manifest = {
    "run_id":       run_id,
    "run_dir":      run_dir,
    "created_at":   datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "current_step": "init",
    "story":        {},
    "artifacts": {
        "research_raw":       f"{run_dir}/research/raw.json",
        "research_validated": f"{run_dir}/research/validated.json",
        "article_raw":        f"{run_dir}/article/raw.md",
        "article_final":      f"{run_dir}/article/final.md",
        "article_with_image": f"{run_dir}/article/with-image.md",
        "docx":               f"{run_dir}/article/article.docx",
        "feature_image":      f"{run_dir}/media/feature.jpg",
        "chart":              f"{run_dir}/media/chart.png",
        "google_drive":       f"{run_dir}/publish/google-drive.json",
        "wordpress":          f"{run_dir}/publish/wordpress.json",
    },
    "steps":   {},
    "checks":  {},
    "results": {},
}

tmp = f"{run_dir}/manifest.json.tmp"
with open(tmp, "w") as f:
    json.dump(manifest, f, indent=2)
os.replace(tmp, f"{run_dir}/manifest.json")
PYEOF

# --------------------------------------------------------------------------
# 3. Remove stale real files / old symlinks at legacy /tmp paths,
#    then symlink them all into the active run-bundle.
# --------------------------------------------------------------------------
_symlink() {
    local legacy="$1"
    local target="$2"
    # Remove old real file or symlink (non-fatal)
    rm -f "$legacy" 2>/dev/null || true
    ln -sf "$target" "$legacy"
}

_symlink "/tmp/research.json"           "${RUN_DIR}/research/validated.json"
_symlink "/tmp/researcher-raw.txt"      "${RUN_DIR}/research/raw.json"
_symlink "/tmp/crypto-article-raw.md"   "${RUN_DIR}/article/raw.md"
_symlink "/tmp/crypto-article.md"       "${RUN_DIR}/article/final.md"
_symlink "/tmp/crypto-with-image.md"    "${RUN_DIR}/article/with-image.md"
_symlink "/tmp/crypto-article.docx"     "${RUN_DIR}/article/article.docx"
_symlink "/tmp/crypto-feature.jpg"      "${RUN_DIR}/media/feature.jpg"
_symlink "/tmp/chart.png"               "${RUN_DIR}/media/chart.png"
_symlink "/tmp/pipeline-manifest.json"  "${RUN_DIR}/manifest.json"
_symlink "/tmp/wp-result.json"          "${RUN_DIR}/publish/wordpress.json"
_symlink "/tmp/openclaw_active_url.txt" "${RUN_DIR}/.active_url"

# --------------------------------------------------------------------------
# 4. Active-run pointer and env file (sourced by orchestrator Step 0)
# --------------------------------------------------------------------------
echo "${RUN_DIR}" > /tmp/crypto-active-run

cat > /tmp/crypto-run-env.sh <<ENVEOF
export RUN_ID="${RUN_ID}"
export RUN_DIR="${RUN_DIR}"
export CRYPTO_RUN_DIR="${RUN_DIR}"
export PIPELINE_MANIFEST="${RUN_DIR}/manifest.json"
export ENABLE_ARTICLE_CHARTS="\${ENABLE_ARTICLE_CHARTS:-0}"
ENVEOF

# --------------------------------------------------------------------------
# 5. Prune run dirs older than 7 days (non-fatal)
# --------------------------------------------------------------------------
find /tmp -maxdepth 1 -name 'crypto-run-*' -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true

echo "[INIT] Run bundle ready: ${RUN_DIR}"
echo "[INIT] RUN_ID=${RUN_ID}"
echo "[INIT] Source env: source /tmp/crypto-run-env.sh"

