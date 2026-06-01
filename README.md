# News Agent — OpenClaw Crypto News Pipeline

Portable 7-agent crypto news pipeline for [OpenClaw](https://github.com/openclaw/openclaw). Clone this repo, copy into `~/.openclaw/`, fill in secrets, run `setup.sh`, and start the pipeline via Telegram.

## Agents

| Agent | Persona | Role |
|-------|---------|------|
| `orchestrator` | Nexus | Pipeline controller + Telegram news cards |
| `researcher` | Scout | RSS research |
| `writer` | Quill | Article writing |
| `chart-generator` | — | Article charts |
| `creator` | Pixel | Feature image (Imagen) |
| `publisher` | Press | Google Drive upload |
| `wp-publisher` | Scribe | WordPress publish |

See [AGENT_PIPELINE_REGISTRY.md](AGENT_PIPELINE_REGISTRY.md) for full pipeline reference.

---

## Setup

Complete these steps on a **new OpenClaw host** to get the News Agent running.

### Prerequisites

Install and configure before starting:

- **OpenClaw CLI** — gateway running locally
- **Python 3** and **`sqlite3`** CLI
- **Bifrost/Vertex** — OpenAI-compatible API proxy for Gemini models
- **SearXNG** — web search plugin (configured in `openclaw.json`)
- **`gog` CLI** — Google Drive uploads
- **`pandoc`** — markdown → DOCX conversion
- **WordPress site** — REST API enabled + Application Password
- **Telegram bot** — create via [@BotFather](https://t.me/BotFather)
- **Telegram group** — add the bot, note the group ID (e.g. `-1001234567890`)

---

### Step 1 — Clone and copy files

```bash
git clone https://github.com/SpearmintTechnolgies/News-Agent.git
cd News-Agent

# Copy workspaces, skills, and setup script into OpenClaw home
cp -r workspace-* skills ~/.openclaw/
cp example.openclaw.json setup.sh ~/.openclaw/

# Create openclaw.json from the example template
cp ~/.openclaw/example.openclaw.json ~/.openclaw/openclaw.json
```

---

### Step 2 — Configure `openclaw.json`

Edit `~/.openclaw/openclaw.json` and replace every `YOUR_*` placeholder:

| Placeholder | What to put |
|-------------|-------------|
| `YOUR_GCP_PROJECT_ID` | Your Google Cloud project ID (Vertex/Bifrost) |
| `YOUR_BIFROST_HOST` | Bifrost proxy host (e.g. `localhost` or `172.30.x.x`) |
| `YOUR_AWS_BEDROCK_BEARER_TOKEN` | AWS Bedrock bearer token (if using Bedrock plugin) |
| `YOUR_OPENCLAW_GATEWAY_AUTH_TOKEN` | Token for OpenClaw web UI / gateway auth |
| `YOUR_TELEGRAM_NEWS_BOT_TOKEN_FROM_BOTFATHER` | Bot token from @BotFather |
| `YOUR_TELEGRAM_NEWS_GROUP_ID` | Telegram group ID for news cards (e.g. `-1001234567890`) |

Also replace **`/home/USER`** in all workspace paths with your actual home directory:

```bash
# Example: replace USER with your username
sed -i 's|/home/USER|/home/yourname|g' ~/.openclaw/openclaw.json
```

---

### Step 3 — WordPress credentials

```bash
cp ~/.openclaw/workspace-wp-publisher/TOOLS.md.example \
   ~/.openclaw/workspace-wp-publisher/TOOLS.md
```

Edit `TOOLS.md` with your site URL, username, and app password. Then update the same values in:

- `~/.openclaw/workspace-wp-publisher/skills/wordpress/publish.sh`
- `~/.openclaw/workspace-wp-publisher/skills/wordpress/wp_post_actions.sh`

Look for `YOUR_WORDPRESS_SITE_URL`, `YOUR_WP_USERNAME`, and `YOUR_WP_APP_PASSWORD`.

---

### Step 4 — Google Drive (gog) credentials

Edit these files and replace the placeholders:

- `~/.openclaw/workspace-publisher/SOUL.md`
- `~/.openclaw/workspace-orchestrator/SOUL.md`

| Placeholder | What to put |
|-------------|-------------|
| `YOUR_GOG_KEYRING_PASSWORD` | Password for the gog keyring |
| `YOUR_GOOGLE_ACCOUNT@gmail.com` | Google account used with `gog drive` |

---

### Step 5 — Telegram group config

Edit `~/.openclaw/workspace-orchestrator/config/telegram_card_config.json`:

```json
{
  "group_id": "YOUR_TELEGRAM_NEWS_GROUP_ID",
  "group_name": "news-agent"
}
```

Use the same group ID as in `openclaw.json`.

---

### Step 6 — Initialize databases (`setup.sh`)

Run the setup script to create both SQLite databases and seed topic dedup state:

```bash
bash ~/.openclaw/setup.sh
```

Expected output:

```
[setup] OpenClaw home: /home/yourname/.openclaw
EDITORIAL_DB_OK: /home/yourname/.openclaw/data/editorial.db

Setup complete.
  Editorial DB:       /home/yourname/.openclaw/data/editorial.db
  Article history DB: /home/yourname/.openclaw/article_history.db
```

What `setup.sh` creates:

| File | Purpose |
|------|---------|
| `~/.openclaw/data/editorial.db` | Telegram news cards + editorial feedback (RATE / PUBLISH / EDIT) |
| `~/.openclaw/article_history.db` | Duplicate URL check (7-day rolling window) |
| `workspace-orchestrator/state/recent_topics.json` | Empty `[]` seed if missing (topic dedup) |

Manual DB commands (optional):

```bash
# Init or check editorial DB
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/editorial_db.py init
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/editorial_db.py status
```

---

### Step 7 — Start and run

1. **Start the OpenClaw gateway**
   ```bash
   openclaw gateway start   # or your usual start command
   ```

2. **Trigger the pipeline** — message the News Agent bot on Telegram:
   ```
   run crypto news pipeline
   ```

3. **Editorial feedback** — in the configured Telegram group, use:
   - `RATE 8` — rate the article
   - `PUBLISH` — publish to WordPress (author picker)
   - `EDIT` — suggest edits

See [EDITORIAL_FEEDBACK.md](workspace-orchestrator/EDITORIAL_FEEDBACK.md) for full editorial commands.

---

### Setup checklist

Use this to confirm everything is ready:

- [ ] Cloned repo and copied `workspace-*`, `skills`, `setup.sh` to `~/.openclaw/`
- [ ] `openclaw.json` created from `example.openclaw.json` with all `YOUR_*` filled in
- [ ] `/home/USER` paths updated to your home directory
- [ ] `TOOLS.md` created from example; WP creds set in `publish.sh` and `wp_post_actions.sh`
- [ ] gog credentials set in publisher and orchestrator `SOUL.md`
- [ ] `telegram_card_config.json` group ID set
- [ ] `bash ~/.openclaw/setup.sh` ran successfully
- [ ] OpenClaw gateway started
- [ ] Test message sent to News Agent bot on Telegram

---

## Repo layout

```
News-Agent/
├── setup.sh                  # Step 6 — init SQLite DBs
├── example.openclaw.json     # Step 2 — config template
├── README.md
├── AGENT_PIPELINE_REGISTRY.md
├── PIPELINE_ARCHITECTURE.md
├── skills/chart-generator/
├── workspace-orchestrator/   # Nexus + pipeline scripts + editorial_db.py
├── workspace-researcher/
├── workspace-writer/
├── workspace-chart-generator/
├── workspace-creator/
├── workspace-publisher/
└── workspace-wp-publisher/   # TOOLS.md.example → copy to TOOLS.md locally
```

---

## Docs

- [AGENT_PIPELINE_REGISTRY.md](AGENT_PIPELINE_REGISTRY.md) — canonical agent/step reference
- [PIPELINE_ARCHITECTURE.md](PIPELINE_ARCHITECTURE.md) — architecture overview
- [workspace-orchestrator/EDITORIAL_FEEDBACK.md](workspace-orchestrator/EDITORIAL_FEEDBACK.md) — Telegram RATE/PUBLISH/EDIT behavior
