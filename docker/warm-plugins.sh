#!/usr/bin/env bash
# Bake OpenClaw bundled plugin runtime deps into the image at build time.
set -euo pipefail

export HOME=/home/openclaw
export NODE_ENV=production
export XDG_CONFIG_HOME=/home/openclaw/.config

PKG_ROOT="/usr/local/lib/node_modules/openclaw"
NM_DIR="${PKG_ROOT}/node_modules"
EXT_DIR="${PKG_ROOT}/dist/extensions"
LOG="/tmp/warm-gateway.log"
STABLE_SECS=20
POLL_SECS=2
MAX_WAIT=420

log() { echo "[warm-plugins $(date -Is)] $*"; }

count_packages() {
  if [[ -d "$NM_DIR" ]]; then
    find "$NM_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l
  else
    echo 0
  fi
}

count_extension_node_modules() {
  find "$EXT_DIR" -mindepth 2 -maxdepth 2 -type d -name node_modules 2>/dev/null | wc -l
}

install_log_lines() {
  grep -c "installed bundled runtime deps" "$LOG" 2>/dev/null || true
}

gateway_ready() {
  grep -qE "\\[gateway\\] ready|\\[health-monitor\\] started" "$LOG" 2>/dev/null
}

if [[ ! -w "$PKG_ROOT" ]]; then
  log "ERROR: ${PKG_ROOT} is not writable by $(whoami)"
  exit 1
fi

log "Package root writable. Starting gateway to warm plugin deps..."

: >"$LOG"
openclaw gateway --allow-unconfigured >>"$LOG" 2>&1 &
GW_PID=$!

cleanup() {
  kill "$GW_PID" 2>/dev/null || true
  wait "$GW_PID" 2>/dev/null || true
}
trap cleanup EXIT

last_installs=-1
stable=0
elapsed=0

while (( elapsed < MAX_WAIT )); do
  installs="$(install_log_lines)"

  if [[ "$installs" != "$last_installs" ]]; then
    log "Install activity: ${installs} events (packages=$(count_packages), extension_node_modules=$(count_extension_node_modules))"
    last_installs="$installs"
    stable=0
  fi

  if gateway_ready; then
    stable=$((stable + POLL_SECS))
    if (( stable >= STABLE_SECS )); then
      log "Gateway ready and install activity stable for ${STABLE_SECS}s"
      break
    fi
  else
    stable=0
  fi

  sleep "$POLL_SECS"
  elapsed=$((elapsed + POLL_SECS))
done

if ! gateway_ready; then
  log "ERROR: gateway did not become ready within ${MAX_WAIT}s"
  tail -80 "$LOG" >&2 || true
  exit 1
fi

final_packages="$(count_packages)"
final_extensions="$(count_extension_node_modules)"
if (( final_packages < 20 )); then
  log "ERROR: expected baked node_modules in ${NM_DIR}, found ${final_packages} packages"
  exit 1
fi

log "Warm complete: ${final_packages} root packages, ${final_extensions} extension node_modules dirs, $(install_log_lines) install events"
log "These deps are now baked into the image layer."
