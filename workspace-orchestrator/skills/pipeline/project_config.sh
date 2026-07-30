#!/usr/bin/env bash
# project_config.sh -- Bash-friendly thin wrapper around project_config.py.
#
# Source this file (or call directly) from any bash pipeline script that
# needs to read per-project settings. Picks the project from:
#   1. $PROJECT_SLUG (env)
#   2. $PROJECT_CONFIG (env, abs path to projects/<slug>.json)
#   3. $PIPELINE_MANIFEST's "project" field
#   4. fallback: "coinography"
#
# Usage examples:
#   source ~/.openclaw/workspace-orchestrator/skills/pipeline/project_config.sh
#   wp_url=$(project_cfg_field wordpress.url)
#   wp_pass=$(project_cfg_password)
#
# Or as a one-shot CLI:
#   bash project_config.sh field wordpress.url
#   bash project_config.sh password
#   bash project_config.sh list
#
# Fails loudly if a required value is missing.

set -euo pipefail

_PROJECT_CONFIG_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PROJECT_CONFIG_PY="${_PROJECT_CONFIG_SCRIPT_DIR}/project_config.py"

project_cfg_resolve_slug() {
  if [[ -n "${PROJECT_SLUG:-}" ]]; then
    echo "$PROJECT_SLUG"
    return 0
  fi
  if [[ -n "${PROJECT_CONFIG:-}" && -f "${PROJECT_CONFIG}" ]]; then
    basename "$PROJECT_CONFIG" .json
    return 0
  fi
  if [[ -n "${PIPELINE_MANIFEST:-}" && -f "${PIPELINE_MANIFEST}" ]]; then
    local slug
    slug=$(python3 -c "import json,sys; d=json.load(open('${PIPELINE_MANIFEST}')); print(d.get('project',''))" 2>/dev/null || echo "")
    if [[ -n "$slug" ]]; then
      echo "$slug"
      return 0
    fi
  fi
  echo "coinography"
}

project_cfg_field() {
  local field="$1"
  local slug
  slug="$(project_cfg_resolve_slug)"
  python3 "$_PROJECT_CONFIG_PY" --slug "$slug" --field "$field"
}

project_cfg_password() {
  local slug
  slug="$(project_cfg_resolve_slug)"
  python3 "$_PROJECT_CONFIG_PY" --slug "$slug" --password
}

project_cfg_path() {
  local slug
  slug="$(project_cfg_resolve_slug)"
  echo "${HOME}/.openclaw/projects/${slug}.json"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  cmd="${1:-help}"
  shift || true
  case "$cmd" in
    field)    project_cfg_field "$@" ;;
    password) project_cfg_password ;;
    slug)     project_cfg_resolve_slug ;;
    path)     project_cfg_path ;;
    list)     python3 "$_PROJECT_CONFIG_PY" --list ;;
    *)
      cat >&2 <<USAGE
project_config.sh -- per-project config reader
Usage:
  project_config.sh field <dotted.key>   # print value
  project_config.sh password             # print WP app password
  project_config.sh slug                 # print resolved slug
  project_config.sh path                 # print resolved config path
  project_config.sh list                 # list available project slugs

Environment:
  PROJECT_SLUG      explicit slug override
  PROJECT_CONFIG    explicit path override
  PIPELINE_MANIFEST manifest with "project" field
USAGE
      exit 2
      ;;
  esac
fi
