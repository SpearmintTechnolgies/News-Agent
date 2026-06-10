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
