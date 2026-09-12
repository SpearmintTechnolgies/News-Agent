#!/usr/bin/env bash
# =============================================================================
# init_run.sh -- Initialize one isolated run-bundle for a pipeline execution.
# =============================================================================
# Usage (from orchestrator Step 0 bash block):
#
#   bash ~/.openclaw/workspace-orchestrator/skills/pipeline/init_run.sh [<project_slug>]
#   source /tmp/<project>-run-env.sh
#
# `<project_slug>` defaults to `coinnetwork` (or value of $PROJECT_SLUG env if
# set). It must correspond to a file at ~/.openclaw/projects/<slug>.json.
#
# After sourcing the env file the shell has:
#   $RUN_ID            -- e.g. 20260604-120000
#   $RUN_DIR           -- /tmp/<project>-run-20260604-120000
#   $CRYPTO_RUN_DIR    -- same as RUN_DIR (legacy alias kept for compat)
#   $PIPELINE_MANIFEST -- $RUN_DIR/manifest.json
#   $PROJECT_SLUG      -- e.g. coinnetwork
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

# Windows: `python3` must be CPython, not the Microsoft Store stub (exit 49).
if [[ -x "/c/Users/Aditya Singh/AppData/Local/Programs/Python/Python311/python.exe" ]]; then
  python3() { "/c/Users/Aditya Singh/AppData/Local/Programs/Python/Python311/python.exe" "$@"; }
  export -f python3
fi
if [[ -x "/c/Program Files/Git/bin/bash.exe" ]]; then
  export PATH="/c/Program Files/Git/bin:/c/Program Files/Git/usr/bin:$PATH"
fi

# --------------------------------------------------------------------------
# 1. Resolve project slug (arg > env > default)
# --------------------------------------------------------------------------
PROJECT_SLUG_INPUT="${1:-${PROJECT_SLUG:-coinnetwork}}"

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
RUN_ROOT="${HOME}/.openclaw/runs"
mkdir -p "${RUN_ROOT}"
RUN_DIR="${RUN_ROOT}/${PROJECT_SLUG}-run-${RUN_ID}"

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
  # Create legacy /tmp path pointing at a run-bundle file.
  # On Linux: real symlink. On Windows Git Bash (no symlink privilege): mklink / hardlink /
  # empty placeholder so init_run never aborts under set -e.
  local legacy="$1"
  local target="$2"
  mkdir -p "$(dirname "$legacy")" "$(dirname "$target")" 2>/dev/null || true
  if [[ ! -e "$target" ]]; then
    touch "$target" 2>/dev/null || true
  fi
  rm -f "$legacy" 2>/dev/null || true
  # Prefer native symlinks when available (Linux / MSYS with privilege)
  if ln -sf "$target" "$legacy" 2>/dev/null; then
    return 0
  fi
  # Windows cmd mklink (file symlink; may work with Developer Mode)
  if command -v cygpath >/dev/null 2>&1 && command -v cmd.exe >/dev/null 2>&1; then
    local w_legacy w_target
    w_legacy="$(cygpath -w "$legacy" 2>/dev/null || true)"
    w_target="$(cygpath -w "$target" 2>/dev/null || true)"
    if [[ -n "$w_legacy" && -n "$w_target" ]]; then
      if cmd.exe //c "mklink \"$w_legacy\" \"$w_target\"" >/dev/null 2>&1; then
        return 0
      fi
    fi
  fi
  # Hardlink when same volume and target is a regular file
  if [[ -f "$target" ]] && ln -f "$target" "$legacy" 2>/dev/null; then
    return 0
  fi
  # Last resort: placeholder + sidecar pointer (scripts prefer RUN_DIR when env is sourced)
  : > "$legacy" 2>/dev/null || true
  printf '%s\n' "$target" > "${legacy}.target" 2>/dev/null || true
  echo "[INIT] WARN: could not symlink $legacy -> $target (using placeholder; prefer RUN_DIR paths)" >&2
  return 0
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

# --------------------------------------------------------------------------
# 5b. Resolve frequently-used project fields ONCE and bake them into the env
#     file. Previously Step 0 / 2.2 / 2.4 each shelled out to project_config.py
#     (8-10 subprocesses per story) for the same values; now they are env vars.
#     Best-effort: never abort the run if an optional field is missing.
# --------------------------------------------------------------------------
python3 - "$SCRIPT_DIR" "$PROJECT_CONFIG_PATH" >> "$ENV_FILE_PROJECT" <<'PYEOF' || true
import os, shlex, sys
script_dir, cfg_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
try:
    import project_config as pc
    cfg = pc.load_project_config(path=cfg_path)
except Exception as e:  # noqa: BLE001 - best effort; Step 0 has fallbacks
    print(f"# project field resolution skipped: {e}", flush=True)
    sys.exit(0)

def emit(var, dotted, *, absolute=False):
    val = cfg.get_path(dotted)
    if val is None or not isinstance(val, (str, int, float)):
        return
    val = str(val)
    if absolute and val:
        resolved = pc.resolve_openclaw_path(val)
        if os.path.exists(resolved):
            val = resolved
    print(f"export {var}={shlex.quote(val)}")

emit("GROUP_CHAT_ID", "telegram.group_id")
emit("PROJECT_NAME", "name")
emit("TEMPLATE_PATH", "writer.template_path", absolute=True)
emit("DRIVE_PREFIX", "publisher.drive_doc_prefix")
emit("DRIVE_PARENT", "publisher.drive_parent_id")
emit("DRIVE_ACCT", "publisher.drive_account")
PYEOF

# Run-unique env file (concurrency-safe handle). The per-slug and legacy env
# files above are overwritten by the next run of the same/any project, so a
# concurrent run must source THIS file instead. Its path is also written into
# the run bundle so the dispatcher can hand it to an isolated session.
ENV_FILE_RUN="/tmp/${PROJECT_SLUG}-run-env-${RUN_ID}.sh"
cp -f "$ENV_FILE_PROJECT" "$ENV_FILE_RUN"
echo "$ENV_FILE_RUN" > "${RUN_DIR}/.run_env_path"

# Backward-compat env file path
cp -f "$ENV_FILE_PROJECT" "/tmp/crypto-run-env.sh"
cp -f "$ENV_FILE_PROJECT" "${RUN_ROOT}/${PROJECT_SLUG}-run-env.sh"
echo "${RUN_DIR}" > "${RUN_ROOT}/${PROJECT_SLUG}-active-run"

# --------------------------------------------------------------------------
# 6. Prune old run dirs (per-project + legacy) older than 7 days
# --------------------------------------------------------------------------
find /tmp -maxdepth 1 -name "${PROJECT_SLUG}-run-*" -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
find /tmp -maxdepth 1 -name 'crypto-run-*'          -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
find "${RUN_ROOT}" -maxdepth 1 -name "${PROJECT_SLUG}-run-*" -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
# Prune stale run-unique env files (left behind by old runs).
find /tmp -maxdepth 1 -name "${PROJECT_SLUG}-run-env-*.sh" -type f -mtime +7 -delete 2>/dev/null || true

echo "[INIT] Project:   ${PROJECT_SLUG}"
echo "[INIT] Config:    ${PROJECT_CONFIG_PATH}"
echo "[INIT] Run bundle ready: ${RUN_DIR}"
echo "[INIT] RUN_ID=${RUN_ID}"
echo "[INIT] Source env: source ${ENV_FILE_PROJECT}"
