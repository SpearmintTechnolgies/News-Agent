# News Agent — OpenClaw Crypto News Pipeline

Portable **8-agent** crypto news pipeline for [OpenClaw](https://github.com/openclaw/openclaw). Supports **multi-project publishing** (Coinography + MemeCoinist) from a single Telegram bot. Clone this repo, copy into `~/.openclaw/`, fill in secrets, run `setup.sh`, and start the pipeline.

## Agents

| Agent | Persona | Role |
|-------|---------|------|
| `orchestrator` | Nexus | Pipeline controller + Telegram news cards |
| `researcher` | Scout | RSS headline scan + deep research |
| `picker` | Sieve | WP category classification + N-story diversity selection |
| `writer` | Quill | Article writing |
| `chart-generator` | — | Article charts |
| `creator` | Pixel | Feature image (Imagen) |
| `publisher` | Press | Google Drive upload |
| `wp-publisher` | Scribe | WordPress publish |

See [AGENT_PIPELINE_REGISTRY.md](AGENT_PIPELINE_REGISTRY.md) for full pipeline reference.

## Projects (multi-site)

Publishing targets live in [`projects/`](projects/):

| Slug | Site | Template |
|------|------|----------|
| `coinography` | coinography.com | `workspace-writer/COINOGRAPHY_TEMPLATE.md` |
| `memecoinist` | memecoinist.com | `workspace-mc-writer/MEMECOIN_TEMPLATE.md` |

Trigger examples on Telegram:
```
run pipeline coinography 3
run pipeline memecoinist 2
run pipeline 1          # defaults to coinography
```

WordPress categories are synced per project:
```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/sync_wp_categories.py --slug coinography
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/sync_wp_categories.py --slug memecoinist
```

See [`projects/README.md`](projects/README.md) for project config schema.

---

## Setup

Complete these steps on a **new OpenClaw host** to get the News Agent running.

### Prerequisites

- **OpenClaw CLI** — gateway running locally
- **Python 3** and **`sqlite3`** CLI
- **Bifrost/Vertex** — OpenAI-compatible API proxy for Gemini models
- **SearXNG** — web search plugin (configured in `openclaw.json`)
- **`gog` CLI** — Google Drive uploads
- **`pandoc`** — markdown → DOCX conversion
- **WordPress site(s)** — REST API enabled + Application Password per project
- **Telegram bot** — create via [@BotFather](https://t.me/BotFather)
- **Telegram group** — add the bot, note the group ID (e.g. `-1001234567890`)

---

### Step 1 — Clone and copy files

```bash
git clone https://github.com/SpearmintTechnolgies/News-Agent.git
cd News-Agent

cp -r workspace-* skills projects ~/.openclaw/
cp example.openclaw.json example.exec-approvals.json setup.sh ~/.openclaw/
cp ~/.openclaw/example.openclaw.json ~/.openclaw/openclaw.json
```

---

### Step 2 — Configure `openclaw.json`

Edit `~/.openclaw/openclaw.json` and replace every `YOUR_*` placeholder:

| Placeholder | What to put |
|-------------|-------------|
| `YOUR_GCP_PROJECT_ID` | Google Cloud project ID (Vertex/Bifrost) |
| `YOUR_BIFROST_HOST` | Bifrost proxy host |
| `YOUR_AWS_BEDROCK_BEARER_TOKEN` | AWS Bedrock bearer token (if using Bedrock plugin) |
| `YOUR_OPENCLAW_GATEWAY_AUTH_TOKEN` | Token for OpenClaw web UI / gateway auth |
| `YOUR_TELEGRAM_NEWS_BOT_TOKEN_FROM_BOTFATHER` | Bot token from @BotFather |
| `YOUR_TELEGRAM_NEWS_GROUP_ID` | Telegram group ID for news cards |
| `YOUR_TELEGRAM_USER_ID` | Your Telegram user ID for owner allowlist |

Replace **`/home/USER`** in all workspace paths with your actual home directory.

---

### Step 3 — WordPress credentials (per project)

```bash
mkdir -p ~/.openclaw/credentials/wp

# Create one app-password file per project (never commit these)
nano ~/.openclaw/credentials/wp/coinography.pass
nano ~/.openclaw/credentials/wp/memecoinist.pass

cp ~/.openclaw/workspace-wp-publisher/TOOLS.md.example \
   ~/.openclaw/workspace-wp-publisher/TOOLS.md
```

Edit `projects/coinography.json` and `projects/memecoinist.json` with your site URLs and WP usernames. Sync live categories:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/sync_wp_categories.py --slug coinography
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/sync_wp_categories.py --slug memecoinist
```

---

### Step 4 — Google Drive (gog) credentials

Edit `~/.openclaw/workspace-publisher/SOUL.md` and replace:
- `YOUR_GOG_KEYRING_PASSWORD`
- `YOUR_GOOGLE_ACCOUNT@gmail.com`

Set matching values in each project's `publisher.drive_account` in `projects/<slug>.json`.

---

### Step 5 — Telegram group config

Edit `~/.openclaw/workspace-orchestrator/config/telegram_card_config.json`:

```json
{
  "group_id": "YOUR_TELEGRAM_NEWS_GROUP_ID",
  "group_name": "news-agent",
  "telegram_account": "news"
}
```

---

### Step 6 — Initialize databases (`setup.sh`)

```bash
bash ~/.openclaw/setup.sh
```

Creates editorial DB, article history DB, and seeds topic dedup state.

---

### Step 7 — Start and run

1. Start the OpenClaw gateway
2. Message the News Agent bot on Telegram: `run pipeline coinography 1`
3. Use editorial feedback in the group: `RATE 8`, `PUBLISH`, `EDIT`

See [workspace-orchestrator/EDITORIAL_FEEDBACK.md](workspace-orchestrator/EDITORIAL_FEEDBACK.md).

---

## Repo layout

```
News-Agent/
├── setup.sh
├── example.openclaw.json
├── example.exec-approvals.json
├── README.md
├── AGENT_PIPELINE_REGISTRY.md
├── PIPELINE_ARCHITECTURE.md
├── PIPELINE_DOCS/
├── projects/                    # coinography.json, memecoinist.json
├── skills/chart-generator/
├── workspace-orchestrator/      # Nexus + pipeline scripts
├── workspace-researcher/
├── workspace-picker/            # Sieve — WP category picker
├── workspace-writer/
├── workspace-mc-writer/           # MemeCoinist article template only
├── workspace-chart-generator/
├── workspace-creator/
├── workspace-publisher/
└── workspace-wp-publisher/
```

---

## Docs

- [AGENT_PIPELINE_REGISTRY.md](AGENT_PIPELINE_REGISTRY.md) — canonical agent/step reference
- [PIPELINE_ARCHITECTURE.md](PIPELINE_ARCHITECTURE.md) — architecture overview
- [projects/README.md](projects/README.md) — project config schema
- [PIPELINE_DOCS/coinography-wordpress-api-integration.md](PIPELINE_DOCS/coinography-wordpress-api-integration.md) — WP REST API guide
