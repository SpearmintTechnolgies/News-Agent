# News Agent — OpenClaw Crypto News Pipeline

Portable 7-agent crypto news pipeline for [OpenClaw](https://github.com/openclaw/openclaw). Clone this repo, copy into `~/.openclaw/`, fill in secrets, and run via Telegram.

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

## Prerequisites

- OpenClaw CLI (gateway running locally)
- Python 3, `sqlite3` CLI
- Bifrost/Vertex (or compatible OpenAI API proxy)
- SearXNG (web search plugin)
- `gog` CLI (Google Drive)
- `pandoc` (DOCX conversion)
- WordPress site with REST API + Application Password

## Install

```bash
git clone https://github.com/SpearmintTechnolgies/News-Agent.git
cd News-Agent

# Copy agent workspaces and skills into OpenClaw home
cp -r workspace-* skills ~/.openclaw/
cp example.openclaw.json ~/.openclaw/openclaw.json
cp setup.sh ~/.openclaw/
```

## Configure secrets

Edit `~/.openclaw/openclaw.json` and replace every `YOUR_*` placeholder:

| Placeholder | Purpose |
|-------------|---------|
| `YOUR_GCP_PROJECT_ID` | Google Cloud project for Vertex/Bifrost |
| `YOUR_BIFROST_HOST` | Bifrost proxy hostname (e.g. `localhost`) |
| `YOUR_AWS_BEDROCK_BEARER_TOKEN` | AWS Bedrock bearer token (if used) |
| `YOUR_OPENCLAW_GATEWAY_AUTH_TOKEN` | OpenClaw web UI / gateway auth token |
| `YOUR_TELEGRAM_NEWS_BOT_TOKEN_FROM_BOTFATHER` | Telegram bot token from @BotFather |
| `YOUR_TELEGRAM_NEWS_GROUP_ID` | Telegram group ID for news cards (e.g. `-1001234567890`) |

Also replace `/home/USER` in all workspace paths with your actual home directory (e.g. `/home/yourname`).

### WordPress credentials

```bash
cp ~/.openclaw/workspace-wp-publisher/TOOLS.md.example \
   ~/.openclaw/workspace-wp-publisher/TOOLS.md
```

Fill in `TOOLS.md`, then update the same values in:

- `workspace-wp-publisher/skills/wordpress/publish.sh`
- `workspace-wp-publisher/skills/wordpress/wp_post_actions.sh`

### Google Drive (gog)

Update `YOUR_GOG_KEYRING_PASSWORD` and `YOUR_GOOGLE_ACCOUNT@gmail.com` in:

- `workspace-publisher/SOUL.md`
- `workspace-orchestrator/SOUL.md`

Also set `group_id` in `workspace-orchestrator/config/telegram_card_config.json`.

## Database setup

```bash
bash ~/.openclaw/setup.sh
```

This creates:

- `~/.openclaw/data/editorial.db` — Telegram card feedback (RATE / PUBLISH / EDIT)
- `~/.openclaw/article_history.db` — duplicate URL check (7-day window)

## Run

1. Start the OpenClaw gateway
2. Message the News Agent bot on Telegram: `run crypto news pipeline`
3. Editorial feedback (RATE, PUBLISH, EDIT) works in the configured Telegram group

## SQLite systems

| Database | Module | Purpose |
|----------|--------|---------|
| `data/editorial.db` | `editorial_db.py` | News cards + editorial feedback |
| `article_history.db` | `article_history.sh` | Prevent duplicate story URLs |

Manual init:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/editorial_db.py init
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/editorial_db.py status
```

## Repo layout

```
News-Agent/
├── setup.sh
├── example.openclaw.json
├── workspace-orchestrator/   # Nexus + pipeline scripts
├── workspace-researcher/
├── workspace-writer/
├── workspace-chart-generator/
├── workspace-creator/
├── workspace-publisher/
├── workspace-wp-publisher/
└── skills/chart-generator/
```

## Docs

- [AGENT_PIPELINE_REGISTRY.md](AGENT_PIPELINE_REGISTRY.md) — canonical agent/step reference
- [PIPELINE_ARCHITECTURE.md](PIPELINE_ARCHITECTURE.md) — architecture overview
