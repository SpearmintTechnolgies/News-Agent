#!/usr/bin/env bash
# setup.sh — first-run setup for News Agent on a new OpenClaw host
#
# Usage:
#   cp -r workspace-* skills projects ~/.openclaw/
#   cp example.openclaw.json ~/.openclaw/openclaw.json   # then edit placeholders
#   cp example.exec-approvals.json ~/.openclaw/exec-approvals.json
#   bash setup.sh

set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If run from repo root before copying to ~/.openclaw, use repo paths
if [[ -d "$SCRIPT_DIR/workspace-orchestrator" ]]; then
  ORCH="$SCRIPT_DIR/workspace-orchestrator"
  RES="$SCRIPT_DIR/workspace-researcher"
else
  ORCH="$OPENCLAW_HOME/workspace-orchestrator"
  RES="$OPENCLAW_HOME/workspace-researcher"
fi

echo "[setup] OpenClaw home: $OPENCLAW_HOME"

# 1. Editorial DB (Telegram cards + feedback + headline pool)
mkdir -p "$OPENCLAW_HOME/data"
python3 "$ORCH/skills/pipeline/editorial_db.py" init "$OPENCLAW_HOME/data/editorial.db"

# 2. Article history DB (duplicate URL check, 7-day window)
export OPENCLAW_HOME
bash "$RES/skills/history/article_history.sh" check "https://setup-init.local" >/dev/null || true

# 3. Topic dedup seed
STATE="$ORCH/state/recent_topics.json"
if [[ ! -f "$STATE" ]]; then
  mkdir -p "$(dirname "$STATE")"
  echo "[]" > "$STATE"
  echo "[setup] Seeded empty recent_topics.json"
fi

# 4. Project configs (if copied from repo)
if [[ -d "$SCRIPT_DIR/projects" && ! -d "$OPENCLAW_HOME/projects" ]]; then
  cp -r "$SCRIPT_DIR/projects" "$OPENCLAW_HOME/projects"
  echo "[setup] Copied projects/ to $OPENCLAW_HOME/projects"
fi

# 5. WP credentials directory (empty — user fills in .pass files)
mkdir -p "$OPENCLAW_HOME/credentials/wp"
echo "[setup] Create app passwords at: $OPENCLAW_HOME/credentials/wp/<slug>.pass"

echo ""
echo "Setup complete."
echo "  Editorial DB:       $OPENCLAW_HOME/data/editorial.db"
echo "  Article history DB: $OPENCLAW_HOME/article_history.db"
echo "  Project configs:    $OPENCLAW_HOME/projects/"
echo ""
echo "Next steps:"
echo "  1. Edit ~/.openclaw/openclaw.json — replace all YOUR_* placeholders"
echo "  2. cp workspace-wp-publisher/TOOLS.md.example ~/.openclaw/workspace-wp-publisher/TOOLS.md"
echo "  3. Add WP app passwords: credentials/wp/coinography.pass, credentials/wp/memecoinist.pass"
echo "  4. Run: python3 workspace-orchestrator/skills/pipeline/sync_wp_categories.py --slug coinography"
echo "  5. Replace /home/USER with your home path in openclaw.json workspace paths"
echo "  6. (Optional) Install pool scheduler: cp workspace-orchestrator/config/openclaw-pool-scheduler.service ~/.config/systemd/user/ && systemctl --user enable --now openclaw-pool-scheduler"
echo "  7. Start OpenClaw gateway and message the News Agent bot on Telegram"
