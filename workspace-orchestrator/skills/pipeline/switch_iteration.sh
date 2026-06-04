#!/usr/bin/env bash
# =============================================================================
# switch_iteration.sh — Manage per-iteration artifact archives within a single
# pipeline run that publishes multiple stories sequentially (Picker batch).
#
# The orchestrator runs each picked story through the standard pipeline. The
# canonical artifact paths inside $RUN_DIR (research/raw.json, article/raw.md,
# media/feature.jpg, …) are reused across iterations — each iteration starts
# from a clean slate. Before truncating those paths for the next iteration we
# archive them into $RUN_DIR/iter_<N>/ so we have a complete per-story record.
#
# /tmp symlinks point at the canonical paths and never need to change.
#
# Usage:
#   switch_iteration.sh --start <N>            Begin iteration N. If N>1 and
#                                              iter_<N-1>/ doesn't already
#                                              exist, copy current canonical
#                                              files into iter_<N-1>/. Then
#                                              truncate canonical files,
#                                              create iter_<N>/ marker dir,
#                                              and refresh .run_started.
#
#   switch_iteration.sh --archive <N>          Snapshot current canonical files
#                                              into iter_<N>/ (used at end of
#                                              the LAST successful iteration
#                                              before terminal cleanup).
#
#   switch_iteration.sh --reset <N> [--reason TEXT]
#                                              Iteration N failed mid-flight.
#                                              Archive current canonical state
#                                              into iter_<N>_failed/ (with an
#                                              optional reason file) and
#                                              truncate canonical paths so the
#                                              next iteration is clean.
#
# Required env (set by `init_run.sh` + `source /tmp/crypto-run-env.sh`):
#   $RUN_DIR — absolute path of the active run bundle
# =============================================================================

set -euo pipefail

if [[ -z "${RUN_DIR:-}" ]]; then
  echo "ITERATION_ERROR: RUN_DIR not set (source /tmp/crypto-run-env.sh first)" >&2
  exit 1
fi

if [[ ! -d "${RUN_DIR}" ]]; then
  echo "ITERATION_ERROR: RUN_DIR does not exist: ${RUN_DIR}" >&2
  exit 1
fi

# Canonical artifacts that are reset between iterations. (Truncated to empty,
# not deleted, so existing /tmp symlinks remain valid.)
CANONICAL_FILES=(
  "${RUN_DIR}/research/raw.json"
  "${RUN_DIR}/research/validated.json"
  "${RUN_DIR}/article/raw.md"
  "${RUN_DIR}/article/final.md"
  "${RUN_DIR}/article/with-image.md"
  "${RUN_DIR}/article/article.docx"
  "${RUN_DIR}/media/feature.jpg"
  "${RUN_DIR}/media/chart.png"
  "${RUN_DIR}/publish/google-drive.json"
  "${RUN_DIR}/publish/wordpress.json"
  "${RUN_DIR}/publish/news-card.json"
)

# Subdirectories whose layout we mirror inside iter_<N>/
SUBDIRS=("research" "article" "media" "publish")

_archive_into() {
  # _archive_into <archive_dir>
  local dest="$1"
  mkdir -p "$dest"
  for sub in "${SUBDIRS[@]}"; do
    mkdir -p "${dest}/${sub}"
  done
  for f in "${CANONICAL_FILES[@]}"; do
    if [[ -e "$f" ]]; then
      # Strip $RUN_DIR/ prefix to keep the layout
      local rel="${f#${RUN_DIR}/}"
      cp -p "$f" "${dest}/${rel}" 2>/dev/null || true
    fi
  done
  # Also archive the manifest snapshot for traceability
  if [[ -e "${RUN_DIR}/manifest.json" ]]; then
    cp -p "${RUN_DIR}/manifest.json" "${dest}/manifest.snapshot.json" 2>/dev/null || true
  fi
}

_truncate_canonical() {
  for f in "${CANONICAL_FILES[@]}"; do
    if [[ -L "$f" || -e "$f" ]]; then
      # Truncate in place; preserve symlink targets
      : > "$f" 2>/dev/null || true
    fi
  done
}

_touch_started() {
  date +%s > "${RUN_DIR}/.run_started"
}

_die() {
  echo "ITERATION_ERROR: $*" >&2
  exit 1
}

# -----------------------------------------------------------------------------
# Arg parse
# -----------------------------------------------------------------------------
ACTION=""
N=""
REASON=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)   ACTION="start";   N="${2:-}"; shift 2;;
    --archive) ACTION="archive"; N="${2:-}"; shift 2;;
    --reset)   ACTION="reset";   N="${2:-}"; shift 2;;
    --reason)  REASON="${2:-}";  shift 2;;
    *) _die "unknown arg: $1";;
  esac
done

if [[ -z "$ACTION" || -z "$N" ]]; then
  _die "usage: switch_iteration.sh --start|--archive|--reset <N> [--reason TEXT]"
fi

if ! [[ "$N" =~ ^[0-9]+$ ]] || [[ "$N" -lt 1 ]]; then
  _die "N must be a positive integer, got: $N"
fi

case "$ACTION" in
  start)
    PREV=$((N - 1))
    if [[ "$PREV" -ge 1 ]]; then
      PREV_DIR="${RUN_DIR}/iter_${PREV}"
      # Always (over)write the previous iteration's archive — current
      # canonical state is iteration PREV's work product.
      _archive_into "$PREV_DIR"
      echo "ITERATION_ARCHIVED: iter_${PREV} <- canonical"
    fi
    _truncate_canonical
    _touch_started
    echo "ITERATION_STARTED: iter_${N}"
    ;;

  archive)
    DEST="${RUN_DIR}/iter_${N}"
    _archive_into "$DEST"
    echo "ITERATION_ARCHIVED: iter_${N} <- canonical"
    ;;

  reset)
    FAIL_DIR="${RUN_DIR}/iter_${N}_failed"
    # If we already have a failed dir for this iteration, append a counter
    SUFFIX=""
    while [[ -d "${FAIL_DIR}${SUFFIX}" ]]; do
      SUFFIX="_$((${SUFFIX#_}+1))"
      [[ "$SUFFIX" == "_" ]] && SUFFIX="_2"
    done
    FAIL_DIR="${FAIL_DIR}${SUFFIX}"
    _archive_into "$FAIL_DIR"
    if [[ -n "$REASON" ]]; then
      printf '%s\n' "$REASON" > "${FAIL_DIR}/REASON.txt"
    fi
    _truncate_canonical
    _touch_started
    echo "ITERATION_RESET: iter_${N} archived to $(basename "$FAIL_DIR")"
    ;;
esac
