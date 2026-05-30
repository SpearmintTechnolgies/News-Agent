#!/usr/bin/env bash
# update_manifest_step.sh — Update one step in a backlink run manifest.
set -euo pipefail

MANIFEST=""
STEP=""
STATUS=""
ARTIFACTS_JSON="{}"

usage() {
  cat <<'EOF'
Usage: update_manifest_step.sh --manifest PATH --step NAME --status STATUS [--artifacts-json JSON]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --step) STEP="$2"; shift 2 ;;
    --status) STATUS="$2"; shift 2 ;;
    --artifacts-json) ARTIFACTS_JSON="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$MANIFEST" || -z "$STEP" || -z "$STATUS" ]]; then
  usage
  exit 1
fi

python3 - <<PYEOF
import json, datetime, os, sys

manifest_path = "$MANIFEST"
step = "$STEP"
status = "$STATUS"
artifacts = json.loads('''$ARTIFACTS_JSON''')

with open(manifest_path, encoding="utf-8") as fh:
    manifest = json.load(fh)

steps = manifest.setdefault("steps", {})
if step not in steps:
    print(f"STEP_FAIL: unknown step {step!r}", file=sys.stderr)
    raise SystemExit(1)

entry = steps[step]
entry["status"] = status
entry["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
if artifacts:
    entry.setdefault("artifacts", {}).update(artifacts)

manifest["current_step"] = step
manifest["updated_at"] = entry["updated_at"]

tmp = manifest_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
os.replace(tmp, manifest_path)

print(f"MANIFEST_UPDATED: step={step} status={status}")
PYEOF
