# Runbook & Troubleshooting

## 🚀 How to Run the Pipeline

To execute the fully autonomous pipeline, open a WSL terminal and run:

```bash
# 1. Clear the Orchestrator's memory to ensure a fresh run
rm -f ~/.openclaw/agents/orchestrator/sessions/sessions.json

# 2. Trigger the Orchestrator
openclaw agent --agent orchestrator --message "Run the crypto news pipeline."
```

*(Note: Do not use the `--deliver` flag unless you specifically have Telegram/Slack routing configured and want the output sent there).*

---

## 🛑 Gateway Management

If OpenClaw is acting strangely, falling back to "embedded mode," or you need to refresh your environment variables, manage the Gateway:

**To Start the Gateway (Foreground):**
```bash
openclaw gateway
```
*(Leave this terminal open, and run the pipeline command in a new tab).*

**To Stop the Gateway:**
```bash
openclaw gateway stop
```

### Fixing a "Stuck" Gateway
If `openclaw gateway stop` says the service is disabled, but starting it says `Port 18789 is already in use (pid XXXX)`, the gateway is stuck.
**Fix:**
```bash
kill -9 [PID_NUMBER]
```
Then restart it normally.

---

## Pool scheduler (scanner + daily feed)

The approve-title-first flow runs `pool_scheduler.py` as a long-lived Python process (scanner every 30m, daily feed card, 48h idle watchdog). It uses **zero LLM tokens**.

**Start or ensure it is running (WSL / dev):**
```bash
chmod +x ~/.openclaw/workspace-orchestrator/skills/pipeline/ensure_scheduler.sh
~/.openclaw/workspace-orchestrator/skills/pipeline/ensure_scheduler.sh
```

**Watchdog:** The `news-scanner` agent has a 30m heartbeat that runs `ensure_scheduler.sh` if the gateway is up. It only starts the scheduler when it is not already running.

**Check status:**
```bash
pgrep -af pool_scheduler.py
tail -20 ~/.openclaw/logs/pool-scheduler.log
```

**Feed time:** Set via env in `ensure_scheduler.sh` (defaults `FEED_HOUR=11`, `FEED_MIN=30` for testing). For production use `FEED_HOUR=10` `FEED_MIN=0`.

---

## Deploy to VPS (GCloud / Linux with systemd)

On a VPS, use **systemd** as the primary supervisor (zero tokens, auto-start on boot, auto-restart on crash).

**1. OpenClaw gateway**
```bash
openclaw gateway install
sudo systemctl enable --now openclaw-gateway
```

**2. Pool scheduler**
```bash
sudo cp ~/.openclaw/workspace-orchestrator/config/openclaw-pool-scheduler.service /etc/systemd/system/
# Edit Environment=FEED_HOUR=10 (and FEED_MIN=0) in the unit if needed
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-pool-scheduler
```

**3. Optional:** Keep the `news-scanner` heartbeat in `openclaw.json` as a secondary safety net.

**Verify on VPS:**
```bash
systemctl status openclaw-gateway openclaw-pool-scheduler
pgrep -af pool_scheduler.py
```

---

## ⚠️ Common Errors

### 1. `HTTP 401: User not found`
- **Where it happens:** Immediately when triggering an agent.
- **Cause:** OpenClaw is hitting the LLM API (OpenRouter) but the `OPENROUTER_API_KEY` is invalid, expired, or out of credits.
- **Fix:** 
  1. Generate a new key at OpenRouter.
  2. Run `export OPENROUTER_API_KEY="sk-or-v1-..."` in your terminal.
  3. Restart the OpenClaw gateway to pick up the new env variable.

### 2. Leonardo AI API: `Billing Error`
- **Where it happens:** During Pixel (Creator) execution.
- **Cause:** The `LEONARDO_API_KEY` has run out of free-tier credits.
- **Fix:** The pipeline is designed to handle this gracefully! It will return `IMAGE_FAILED`, skip the image embedding step, and upload the text-only article to Google Drive anyway. No action is strictly required unless you want images again (in which case, update the API key in `.bashrc`).

### 3. Google Drive `gog` Authentication Failure
- **Where it happens:** During Press (Publisher) execution.
- **Cause:** `gog` is trying to prompt for a password interactively.
- **Fix:** Ensure the Publisher's SOUL contains `GOG_KEYRING_PASSWORD="YOUR_GOG_KEYRING_PASSWORD"` immediately preceding the `gog drive upload` command.
