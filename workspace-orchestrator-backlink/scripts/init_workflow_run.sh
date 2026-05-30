#!/usr/bin/env bash
# init_workflow_run.sh — Create an isolated backlink pipeline run bundle.
set -euo pipefail

ROOT="${BACKLINK_ROOT:-$HOME/.openclaw/workspace-orchestrator-backlink}"
DATA_DIR="${BACKLINK_DATA_DIR:-$HOME/.openclaw/data/backlink_runs}"
DB_PATH="${BACKLINK_DB:-$HOME/.openclaw/data/backlink_agent.db}"
PROJECT_SLUG="cryptography-com"
SOURCE_URL=""
WORKFLOW_ID=""

usage() {
  cat <<'EOF'
Usage: init_workflow_run.sh [--project-slug SLUG] [--url URL] [--workflow-id WF-ID] [--db PATH]

Creates ~/.openclaw/data/backlink_runs/<WF-ID>/manifest.json and exports env vars.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-slug) PROJECT_SLUG="$2"; shift 2 ;;
    --url) SOURCE_URL="$2"; shift 2 ;;
    --workflow-id) WORKFLOW_ID="$2"; shift 2 ;;
    --db) DB_PATH="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$WORKFLOW_ID" ]]; then
  WORKFLOW_ID="$(python3 "$ROOT/scripts/allocate_workflow_id.py" --db "$DB_PATH")"
fi

RUN_DIR="$DATA_DIR/$WORKFLOW_ID"
mkdir -p "$RUN_DIR"

PHASE="$(python3 -c "import json; print(json.load(open('$ROOT/config/phase.json'))['phase'])")"

python3 - <<PYEOF
import json, datetime, os, sys
from pathlib import Path

root = Path("$ROOT")
sys.path.insert(0, str(root / "database"))
sys.path.insert(0, str(root / "workflows"))
import backlink_db  # noqa: E402

db_path = "$DB_PATH"
backlink_db.ensure_db(db_path)

run_dir = "$RUN_DIR"
workflow_id = "$WORKFLOW_ID"
project_slug = "$PROJECT_SLUG"
source_url = "$SOURCE_URL" or None
phase = int("$PHASE")

manifest = {
    "workflow_id": workflow_id,
    "project_slug": project_slug,
    "phase": phase,
    "source_url": source_url,
    "run_dir": run_dir,
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "current_step": "init",
    "steps": {
        "discover": {"status": "pending", "agent": "bl-discovery", "artifacts": {}},
        "score": {"status": "pending", "agent": "bl-scorer", "artifacts": {}},
        "audit": {"status": "pending", "agent": "bl-auditor", "artifacts": {}},
        "content": {"status": "pending", "agent": "bl-content", "artifacts": {}},
        "approval": {"status": "pending", "agent": "orchestrator-backlink", "artifacts": {}},
    },
}

manifest_path = os.path.join(run_dir, "manifest.json")
tmp_path = manifest_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)
    fh.write("\n")
os.replace(tmp_path, manifest_path)

if source_url:
    cid = backlink_db.get_or_create_campaign("cryptography.com", "cryptography.com", db_path=db_path)
    hash_value = backlink_db.url_hash(source_url)
    existing = backlink_db.find_opportunity_by_url_hash(cid, hash_value, db_path=db_path)
    if existing:
        wf = backlink_db.create_workflow(
            workflow_id,
            campaign_id=cid,
            opportunity_id=existing.id,
            db_path=db_path,
        )
    else:
        _, wf = backlink_db.create_opportunity_and_workflow(
            cid,
            source_url,
            workflow_id,
            db_path=db_path,
        )
    print(f"BOOTSTRAPPED: workflow_id={wf.workflow_id} state={wf.state}")
else:
    print(f"MANIFEST_ONLY: workflow_id={workflow_id} (no URL bootstrap)")

print(f"WORKFLOW_ID={workflow_id}")
print(f"RUN_DIR={run_dir}")
print(f"PIPELINE_MANIFEST={manifest_path}")
PYEOF

ENV_FILE="/tmp/backlink-run-env.sh"
cat > "$ENV_FILE" <<EOF
export WORKFLOW_ID="$WORKFLOW_ID"
export RUN_DIR="$RUN_DIR"
export PIPELINE_MANIFEST="$RUN_DIR/manifest.json"
export BACKLINK_DB="$DB_PATH"
export BACKLINK_PROJECT_SLUG="$PROJECT_SLUG"
EOF

echo "[init] Run bundle: $RUN_DIR"
echo "[init] Workflow id: $WORKFLOW_ID"
echo "[init] Source env: source $ENV_FILE"
