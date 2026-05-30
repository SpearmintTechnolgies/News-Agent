#!/usr/bin/env bash
# read_manifest.sh — Print manifest summary for orchestrator context.
set -euo pipefail

MANIFEST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: read_manifest.sh --manifest PATH"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$MANIFEST" ]]; then
  echo "Usage: read_manifest.sh --manifest PATH" >&2
  exit 1
fi

python3 - <<PYEOF
import json, sys

with open("$MANIFEST", encoding="utf-8") as fh:
    manifest = json.load(fh)

print(f"workflow_id={manifest.get('workflow_id')}")
print(f"project_slug={manifest.get('project_slug')}")
print(f"phase={manifest.get('phase')}")
print(f"current_step={manifest.get('current_step')}")
print(f"source_url={manifest.get('source_url')}")

for name, step in manifest.get("steps", {}).items():
    print(f"step.{name}={step.get('status')} agent={step.get('agent')}")
PYEOF
