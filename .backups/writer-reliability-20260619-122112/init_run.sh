#!/usr/bin/env bash
# =============================================================================
# init_run.sh -- Initialize one isolated run-bundle for a pipeline execution.
# =============================================================================
# Usage (from orchestrator Step 0 bash block):
#
#   bash ~/.openclaw/workspace-orchestrator/skills/pipeline/init_run.sh [<project_slug>]
#   source /tmp/<project>-run-env.sh
#
# `<project_slug>` defaults to `coinography` (or value of $PROJECT_SLUG env if
# set). It must correspond to a file at ~/.openclaw/projects/<slug>.json.
#
# After sourcing the env file the shell has:
#   $RUN_ID            -- e.g. 20260604-120000
#   $RUN_DIR           -- /tmp/<project>-run-20260604-120000
#   $CRYPTO_RUN_DIR    -- same as RUN_DIR (legacy alias kept for compat)
#   $PIPELINE_MANIFEST -- $RUN_DIR/manifest.json
#   $PROJECT_SLUG      -- e.g. coinography
#   $PROJECT_CONFIG    -- absolute path to projects/<slug>.json
#
# The run-bundle layout (unchanged from before, just under a project-prefixed dir):
#   $RUN_DIR/
#     manifest.json
#     .run_started
#     research/ picker/ article/ media/ publish/ ...
#
# Backward-compat symlinks at /tmp/crypto-* are created ONLY when project is
# coinography, so all legacy code paths keep working. For non-coinography
# projects we use /tmp/<project>-* symlinks instead.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------------
# 1. Resolve project slug (arg > env > default)
# --------------------------------------------------------------------------
PROJECT_SLUG_INPUT="${1:-${PROJECT_SLUG:-coinography}}"

# Validate the project exists; project_config.py prints a clear error if not.
PROJECT_CONFIG_PATH="${HOME}/.openclaw/projects/${PROJECT_SLUG_INPUT}.json"
if [[ ! -f "$PROJECT_CONFIG_PATH" ]]; then
  echo "[INIT] ERROR: project config not found: $PROJECT_CONFIG_PATH" >&2
  echo "[INIT] Available projects:" >&2
  python3 "${SCRIPT_DIR}/project_config.py" --list >&2 || true
  exit 1
fi

PROJECT_SLUG="$(python3 "${SCRIPT_DIR}/project_config.py" --slug "$PROJECT_SLUG_INPUT" --field slug)"

# RUN_ID must be globally unique so two runs started in the same second (or two
# projects running concurrently) never share a RUN_DIR. Timestamp + PID + random
# suffix. Nothing parses RUN_ID as a strict timestamp; it is only used in paths
# and labels.
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$-${RANDOM}"
RUN_DIR="/tmp/${PROJECT_SLUG}-run-${RUN_ID}"

# --------------------------------------------------------------------------
# 2. Create nested directory tree + empty placeholder files
# --------------------------------------------------------------------------
mkdir -p \
  "${RUN_DIR}/research" \
  "${RUN_DIR}/article" \
  "${RUN_DIR}/media" \
  "${RUN_DIR}/publish" \
  "${RUN_DIR}/picker"

touch \
  "${RUN_DIR}/research/raw.json" \
  "${RUN_DIR}/research/validated.json" \
  "${RUN_DIR}/research/headlines.json" \
  "${RUN_DIR}/picker/picker_input.json" \
  "${RUN_DIR}/picker/picks.json" \
  "${RUN_DIR}/article/raw.md" \
  "${RUN_DIR}/article/final.md" \
  "${RUN_DIR}/article/with-image.md" \
  "${RUN_DIR}/media/feature.jpg" \
  "${RUN_DIR}/media/chart.png" \
  "${RUN_DIR}/publish/news-card.json"

date +%s > "${RUN_DIR}/.run_started"

# --------------------------------------------------------------------------
# 3. Write manifest.json (now carries project + project_config_path)
# --------------------------------------------------------------------------
python3 - "$RUN_ID" "$RUN_DIR" "$PROJECT_SLUG" "$PROJECT_CONFIG_PATH" <<'PYEOF'
import datetime, json, os, sys

run_id, run_dir, project, project_cfg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

manifest = {
    "run_id":              run_id,
    "run_dir":             run_dir,
    "project":             project,
    "project_config_path": project_cfg,
    "created_at":          datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "current_step":        "init",
    "story":               {},
    "batch": {
        "target_count":   1,
        "pick_run_id":    "",
        "current_pick":   0,
        "completed_picks": [],
    },
    "artifacts": {
        "research_raw":       f"{run_dir}/research/raw.json",
        "research_validated": f"{run_dir}/research/validated.json",
        "headlines":          f"{run_dir}/research/headlines.json",
        "picker_input":       f"{run_dir}/picker/picker_input.json",
        "picks":              f"{run_dir}/picker/picks.json",
        "article_raw":        f"{run_dir}/article/raw.md",
        "article_final":      f"{run_dir}/article/final.md",
        "article_with_image": f"{run_dir}/article/with-image.md",
        "docx":               f"{run_dir}/article/article.docx",
        "feature_image":      f"{run_dir}/media/feature.jpg",
        "chart":              f"{run_dir}/media/chart.png",
        "google_drive":       f"{run_dir}/publish/google-drive.json",
        "wordpress":          f"{run_dir}/publish/wordpress.json",
        "news_card":          f"{run_dir}/publish/news-card.json",
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
# 4. Symlinks: legacy /tmp paths -> run-bundle files
# --------------------------------------------------------------------------
_symlink() {
  local legacy="$1"
  local target="$2"
  rm -f "$legacy" 2>/dev/null || true
  ln -sf "$target" "$legacy"
}

# Project-prefixed symlinks (new, canonical for non-coinography projects)
_symlink "/tmp/${PROJECT_SLUG}-research.json"      "${RUN_DIR}/research/validated.json"
_symlink "/tmp/${PROJECT_SLUG}-researcher-raw.txt" "${RUN_DIR}/research/raw.json"
_symlink "/tmp/${PROJECT_SLUG}-headlines.json"     "${RUN_DIR}/research/headlines.json"
_symlink "/tmp/${PROJECT_SLUG}-picker-input.json"  "${RUN_DIR}/picker/picker_input.json"
_symlink "/tmp/${PROJECT_SLUG}-picks.json"         "${RUN_DIR}/picker/picks.json"
_symlink "/tmp/${PROJECT_SLUG}-article-raw.md"     "${RUN_DIR}/article/raw.md"
_symlink "/tmp/${PROJECT_SLUG}-article.md"         "${RUN_DIR}/article/final.md"
_symlink "/tmp/${PROJECT_SLUG}-with-image.md"      "${RUN_DIR}/article/with-image.md"
_symlink "/tmp/${PROJECT_SLUG}-article.docx"       "${RUN_DIR}/article/article.docx"
_symlink "/tmp/${PROJECT_SLUG}-feature.jpg"        "${RUN_DIR}/media/feature.jpg"
_symlink "/tmp/${PROJECT_SLUG}-chart.png"          "${RUN_DIR}/media/chart.png"
_symlink "/tmp/${PROJECT_SLUG}-pipeline-manifest.json" "${RUN_DIR}/manifest.json"
_symlink "/tmp/${PROJECT_SLUG}-wp-result.json"     "${RUN_DIR}/publish/wordpress.json"
_symlink "/tmp/${PROJECT_SLUG}-active-url.txt"     "${RUN_DIR}/.active_url"

# Backward-compat: keep the /tmp/crypto-* + /tmp/openclaw_* + /tmp/research.json
# names pointing at the *currently active* run bundle so any worker/SOUL/script
# that still references the legacy paths keeps working unchanged.
_symlink "/tmp/research.json"           "${RUN_DIR}/research/validated.json"
_symlink "/tmp/researcher-raw.txt"      "${RUN_DIR}/research/raw.json"
_symlink "/tmp/headlines.json"          "${RUN_DIR}/research/headlines.json"
_symlink "/tmp/picker-input.json"       "${RUN_DIR}/picker/picker_input.json"
_symlink "/tmp/picks.json"              "${RUN_DIR}/picker/picks.json"
_symlink "/tmp/crypto-article-raw.md"   "${RUN_DIR}/article/raw.md"
_symlink "/tmp/crypto-article.md"       "${RUN_DIR}/article/final.md"
_symlink "/tmp/crypto-with-image.md"    "${RUN_DIR}/article/with-image.md"
_symlink "/tmp/crypto-article.docx"     "${RUN_DIR}/article/article.docx"
_symlink "/tmp/crypto-feature.jpg"      "${RUN_DIR}/media/feature.jpg"
_symlink "/tmp/chart.png"               "${RUN_DIR}/media/chart.png"
_symlink "/tmp/pipeline-manifest.json"  "${RUN_DIR}/manifest.json"
_symlink "/tmp/openclaw-active-manifest.json" "${RUN_DIR}/manifest.json"
_symlink "/tmp/wp-result.json"          "${RUN_DIR}/publish/wordpress.json"
_symlink "/tmp/openclaw_active_url.txt" "${RUN_DIR}/.active_url"

# --------------------------------------------------------------------------
# 5. Active-run pointer + env file (sourced by orchestrator Step 0)
# --------------------------------------------------------------------------
# Per-project pointer (each project has its own active run; in v1 we run serial
# so there will only ever be one truly active at a time, but per-project
# pointers also make debugging multi-project sessions easier).
echo "${RUN_DIR}" > "/tmp/${PROJECT_SLUG}-active-run"
# Legacy pointer kept for backward compat:
echo "${RUN_DIR}" > /tmp/crypto-active-run

# Env file (project-prefixed canonical name + legacy alias).
ENV_FILE_PROJECT="/tmp/${PROJECT_SLUG}-run-env.sh"
cat > "$ENV_FILE_PROJECT" <<ENVEOF
export RUN_ID="${RUN_ID}"
export RUN_DIR="${RUN_DIR}"
export CRYPTO_RUN_DIR="${RUN_DIR}"
export PIPELINE_MANIFEST="${RUN_DIR}/manifest.json"
export PROJECT_SLUG="${PROJECT_SLUG}"
export PROJECT_CONFIG="${PROJECT_CONFIG_PATH}"
export ENABLE_ARTICLE_CHARTS="\${ENABLE_ARTICLE_CHARTS:-0}"
ENVEOF

# Run-unique env file (concurrency-safe handle). The per-slug and legacy env
# files above are overwritten by the next run of the same/any project, so a
# concurrent run must source THIS file instead. Its path is also written into
# the run bundle so the dispatcher can hand it to an isolated session.
ENV_FILE_RUN="/tmp/${PROJECT_SLUG}-run-env-${RUN_ID}.sh"
cp -f "$ENV_FILE_PROJECT" "$ENV_FILE_RUN"
echo "$ENV_FILE_RUN" > "${RUN_DIR}/.run_env_path"

# Backward-compat env file path
cp -f "$ENV_FILE_PROJECT" "/tmp/crypto-run-env.sh"

# --------------------------------------------------------------------------
# 6. Prune old run dirs (per-project + legacy) older than 7 days
# --------------------------------------------------------------------------
find /tmp -maxdepth 1 -name "${PROJECT_SLUG}-run-*" -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
find /tmp -maxdepth 1 -name 'crypto-run-*'          -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
# Prune stale run-unique env files (left behind by old runs).
find /tmp -maxdepth 1 -name "${PROJECT_SLUG}-run-env-*.sh" -type f -mtime +7 -delete 2>/dev/null || true

echo "[INIT] Project:   ${PROJECT_SLUG}"
echo "[INIT] Config:    ${PROJECT_CONFIG_PATH}"
echo "[INIT] Run bundle ready: ${RUN_DIR}"
echo "[INIT] RUN_ID=${RUN_ID}"
echo "[INIT] Source env: source ${ENV_FILE_PROJECT}"
