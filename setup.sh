#!/usr/bin/env bash
# setup.sh — first-run setup for News Agent on a new OpenClaw host
#
# Usage:
#   cp -r workspace-* skills ~/.openclaw/
#   cp example.openclaw.json ~/.openclaw/openclaw.json   # then edit placeholders
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

# 1. Editorial DB (Telegram cards + feedback)
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

echo ""
echo "Setup complete."
echo "  Editorial DB:       $OPENCLAW_HOME/data/editorial.db"
echo "  Article history DB: $OPENCLAW_HOME/article_history.db"
echo ""
echo "Next steps:"
echo "  1. Edit ~/.openclaw/openclaw.json — replace all YOUR_* placeholders"
echo "  2. cp workspace-wp-publisher/TOOLS.md.example ~/.openclaw/workspace-wp-publisher/TOOLS.md"
echo "  3. Update WP credentials in TOOLS.md and publish.sh / wp_post_actions.sh"
echo "  4. Replace /home/USER with your home path in openclaw.json workspace paths"
echo "  5. Start OpenClaw gateway and message the News Agent bot on Telegram"
