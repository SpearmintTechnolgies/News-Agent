# Agent Pipeline Registry

**Last updated:** 2026-06-03  
**Purpose:** Canonical living reference for the OpenClaw crypto news pipeline — all agents, subagents, prompts, tools, skills, and pipeline steps.  
**Config source of truth:** [`openclaw.json`](openclaw.json)

> **Secrets policy:** This file documents *where* credentials live (`TOOLS.md`, `openclaw.json`, shell scripts) but never copies tokens, passwords, or API keys.

---

## Maintenance triggers

Update this registry in the **same change** whenever you edit any of:

| Category | Paths |
|----------|-------|
| Config | `openclaw.json`, `exec-approvals.json` |
| Prompts | `workspace-*/SOUL.md`, `workspace-*/AGENTS.md`, `workspace-*/IDENTITY.md`, `workspace-*/TOOLS.md`, `workspace-writer/COINOGRAPHY_TEMPLATE.md` |
| Skills | `workspace-*/skills/**`, `skills/**` |
| Pipeline scripts | `workspace-orchestrator/skills/pipeline/**` |

On each update: bump **Last updated**, edit only affected sections, append one line to **Change log**.

---

## Architecture

```mermaid
flowchart TD
    TG[TelegramUser] -->|run pipeline N| NX[Nexus_orchestrator]
    NX --> S0[Step0_init_run]
    S0 --> S05[Step0_5_parse_N]
    S05 --> S1A[Step1a_Scout_HEADLINE_SCAN]
    S1A --> S1B[Step1b_build_picker_input]
    S1B --> S1C[Step1c_Sieve_picker]
    S1C --> V1C[validate_picks_DB_insert]
    V1C --> LOOP{For each pick 1..K}
    LOOP --> S20[Step2_0_switch_iteration_start]
    S20 --> S21[Step2_1_Scout_DEEP_RESEARCH]
    S21 --> V21[validate_research_topic_dedup]
    V21 --> S22[Step2_2_Quill_writer]
    S22 --> V22[sync_structure_anchor_validators]
    V22 --> S23[Step2_3_Pixel_creator]
    S23 --> S24[Step2_4_Press_drive_upload]
    S24 --> S25[Step2_5_user_yes_no_stop]
    S25 -->|yes| S26[Step2_6_Scribe_wp_publisher_card]
    S25 -->|no| LOOP_NEXT
    S25 -->|stop| S3
    S26 --> LOOP_NEXT[next pick]
    LOOP_NEXT --> LOOP
    LOOP --> S3[Step3_final_batch_report_cleanup]
    S26 --> TGCard[Telegram_news_agent_group]
    TGCard --> Editorial[handle_card_feedback]
```

**Delegation:** `sessions_spawn` + `sessions_yield` only. Do **not** use `openclaw agent ... --deliver` in bash.

**Handoffs:** File-first. Workers write to `/tmp/crypto-run-<RUN_ID>/` (symlinked from `/tmp/...`). Chat returns `"SUCCESS"` or structured status (`CHART_DONE:`, `WP_FAILED:`). Nexus validates disk artifacts with Python/bash scripts.

**Multi-story batch model:** A single run may publish N stories (1 ≤ N ≤ 10). The Researcher is invoked twice per run: once in `MODE: HEADLINE_SCAN` to produce ~10 candidate headlines, then once per pick in `MODE: DEEP_RESEARCH`. The new `picker` agent (Sieve) classifies each candidate into one of 8 categories and selects N picks balancing freshness with category diversity. Within the per-pick loop the existing Writer / Creator / Publisher / WP-Publisher agents are reused unchanged.

**Entry point:** Telegram bound exclusively to `orchestrator` (`openclaw.json` → `bindings`).

---

## Global configuration

| Setting | Value |
|---------|-------|
| Default model | `local-bifrost/gpt-5.4-mini` |
| Default workspace | `/home/bhard/.openclaw/workspace` |
| LLM backend | Bifrost Azure proxy (`local-bifrost` provider in `openclaw.json`) |
| Tools profile | `coding` |
| Exec security | `full`, `ask: off` |
| Context injection | `always` |
| Web search | SearXNG plugin (`paulgo.io`) |
| Hooks | `session-memory`, `boot-md` |
| Gateway | Local port 18789, token auth |

---

## Agent registry matrix

| Agent ID | Persona | Workspace | Model | Pipeline step |
|----------|---------|-----------|-------|---------------|
| `main` | (default) | `workspace/` | gpt-5.4-mini | Not in pipeline |
| `orchestrator` | Nexus | `workspace-orchestrator/` | gpt-5.4 | Controller (Steps 0–3) |
| `researcher` | Scout | `workspace-researcher/` | gpt-5.4 | Step 1a (HEADLINE_SCAN) + Step 2.1 (DEEP_RESEARCH per pick) |
| `picker` | Sieve | `workspace-picker/` | gpt-5.4 | Step 1c — Categorize + select N picks |
| `writer` | Quill | `workspace-writer/` | gpt-5.4 | Step 2.2 — Write |
| `chart-generator` | Pixel | `workspace-chart-generator/` | gpt-5.4-mini | Step 2.3 (chart sub-step, optional) |
| `creator` | Pixel | `workspace-creator/` | gpt-5.4-mini | Step 2.3 — Feature image |
| `publisher` | Press | `workspace-publisher/` | gpt-5.4-mini | Step 2.4 — Google Drive |
| `wp-publisher` | Scribe | `workspace-wp-publisher/` | gpt-5.4-mini | Step 2.6 — WordPress (user-gated) |

**Orchestrator subagent allowlist:** `researcher`, `picker`, `writer`, `chart-generator`, `creator`, `publisher`, `wp-publisher`

---

## Prompt assembly

OpenClaw injects workspace markdown at session start:

| File | Role |
|------|------|
| `SOUL.md` | Primary behavioral contract (the real prompt) |
| `AGENTS.md` | Universal safety / worker rules |
| `IDENTITY.md` | Name, role, emoji, vibe |
| `TOOLS.md` | Environment notes, RSS feeds, API config (secrets live here) |
| `USER.md` | Human preferences |
| `HEARTBEAT.md` | Periodic checks (mostly empty) |
| `BOOTSTRAP.md` | First-run setup (if present) |

Plus runtime base text and an injected skill catalog from `SKILL.md` files.

---

## Per-agent profiles

### Orchestrator — Nexus

| Field | Value |
|-------|-------|
| Workspace | `workspace-orchestrator/` |
| SOUL | `workspace-orchestrator/SOUL.md` (~440 lines) |
| Role | Sequence workers, validate every step, never write/publish content |
| Model | gpt-5.4 |

**Tools (config extras):** `agents_list`, `nodes`, `message`, `gateway`, `browser`, `canvas`, `tts`, `sessions_spawn`, `sessions_yield`, `subagents`

**Full runtime tools:** `read`, `write`, `edit`, `exec`, `process`, `canvas`, `message`, `tts`, `image_generate`, `agents_list`, `sessions_list`, `sessions_history`, `sessions_send`, `sessions_spawn`, `sessions_yield`, `subagents`, `session_status`, `web_search`, `web_fetch`, `browser`, `memory_search`, `memory_get`

**Pipeline steps:**

| Step | Action |
|------|--------|
| 0 | `init_run.sh` → run bundle at `/tmp/crypto-run-<RUN_ID>/` (now also creates `picker/` + `iter_<N>/` archive layout) |
| 0.5 | Parse `N` (story count) from user command (`run pipeline N`); persist into `manifest.batch.target_count` and `pick_run_id` |
| 1a | Spawn researcher in `MODE: HEADLINE_SCAN` → up to 10 fresh candidates → `validate_headlines.py` |
| 1b | `build_picker_input.py` — assemble `picker_input.json` with `target_count` + `recent_categories` (24h) + consumed-URL filter |
| 1c | Spawn `picker` (Sieve) → `picks.json` → `validate_picks.py` inserts each pick into `picked_stories` table |
| 2.0 | `switch_iteration.sh --start <N>` (per pick) — archive previous iter, truncate canonical files |
| 2.1 | Spawn researcher in `MODE: DEEP_RESEARCH` for the current pick → `validate_research.py` (with `--raw-path`/`--validated-path` for per-iteration files when using sub-bundles) → 24h topic dedup |
| 2.2 | Spawn writer → sync/validate article (structure, anchors, word count) — same retry/repair contract as before |
| 2.3 | Spawn creator → validate JPEG (chart sub-step still gated by `ENABLE_ARTICLE_CHARTS`) |
| 2.4 | Build DOCX with pandoc → `verify_artifacts.py --stage pre_drive` → spawn publisher for Drive upload |
| 2.5 | Per-story user gate — reply with headline + category + Drive link, **STOP** for `yes`/`no`/`stop` |
| 2.6 | If yes → spawn wp-publisher → `update_recent_topics.py --status drafted` → `build_and_send_card.py` → `update_pick_status.py --status published` |
|     | If no → `update_pick_status.py --status cancelled` and continue to next pick |
|     | If stop → cancel all remaining picks and break to Step 3 |
| 3 | Final batch report (per-pick statuses from `picked_stories`) → `cleanup_run_artifacts.sh` (ONCE per batch) |

**Spawn message templates:**

- **researcher (HEADLINE_SCAN):** `MODE: HEADLINE_SCAN` / `OUTPUT_FILE: $RUN_DIR/research/headlines.json` / `TARGET_COUNT: 10`
- **researcher (DEEP_RESEARCH):** `MODE: DEEP_RESEARCH` / `INPUT_FILE: $RUN_DIR/picker/picks.json` / `PICK_INDEX: <N>` / `OUTPUT_FILE: $RUN_DIR/research/raw.json`
- **picker:** `INPUT_FILE: $RUN_DIR/picker/picker_input.json` / `OUTPUT_FILE: $RUN_DIR/picker/picks.json`
- **writer:** Read `validated.json` + `COINOGRAPHY_TEMPLATE.md`; 1000–1200 body words; write to `$RUN_DIR/article/raw.md`; yield `SUCCESS` only.
- **chart-generator:** `CHART_COIN: [coin]` / `CHART_DAYS: 30` / `CHART_OUTPUT: $RUN_DIR/media/chart.png`
- **creator:** Generate feature image from `validated.json` using Scene Formula in SOUL; run `generate.sh`.
- **publisher:** Run exact `gog drive upload /tmp/crypto-article.docx ...`; return `webViewLink`.
- **wp-publisher:** Save WordPress draft from `/tmp/crypto-article.md` + `/tmp/crypto-feature.jpg` (live via Telegram card Publish).

**Key rules:** Writer told max 1200 words; sync accepts up to 1300 (+100 buffer, never tell writer). Must see `ARTICLE_SYNCED` + `ARTIFACTS_OK: post_sync` before Step 2.3 in each iteration. Per-story user gate at Step 2.5 (`yes`/`no`/`stop`). One bad story does **not** abort the batch — `update_pick_status.py --status failed` + `switch_iteration.sh --reset` + continue. Never hallucinate URLs.

**State:**
- `workspace-orchestrator/state/recent_topics.json` — 24h topic registry (now carries `category`).
- `~/.openclaw/data/editorial.db` — `articles` (with new `category` column) + `picked_stories` (new table).

---

### Researcher — Scout

| Field | Value |
|-------|-------|
| Workspace | `workspace-researcher/` |
| SOUL | `workspace-researcher/SOUL.md` |
| Model | gpt-5.4 |
| Pipeline step | 1a (HEADLINE_SCAN) and 2.1 (DEEP_RESEARCH per pick) |

**Two modes** (selected by the first non-empty line of the spawn message):

| Mode | Job | Output |
|------|-----|--------|
| `HEADLINE_SCAN` | Fetch RSS feeds, parse and dedupe items, return up to 10 fresh candidate headlines (last 24h). No deep extraction. | `$RUN_DIR/research/headlines.json` |
| `DEEP_RESEARCH` | Read the assigned pick from `INPUT_FILE` + `PICK_INDEX`, validate URLs, run `trafilatura` on primary + corroborating sources, build aggregated research JSON (≥600 words). Carries `category` through unchanged from the picker. | `$RUN_DIR/research/raw.json` (or `--validated-path` per-iteration variant) |

**Tools denied:** `web_search`, `web_fetch`

**Tools available:** `read`, `write`, `edit`, `exec`, `process`, `image_generate`, `sessions_yield`, `memory_search`, `memory_get`

**HEADLINE_SCAN required JSON fields:** `status="ok"`, `mode="headline_scan"`, `scanned_at`, `target_count`, `candidate_count`, `candidates[]` (each: `candidate_index`, `headline`, `url`, `pub_date`, `source`, `summary`, `corroborating_sources[]`).

**DEEP_RESEARCH required JSON fields:** `status="ok"`, `mode="deep_research"`, `story_id`, `category`, `topic_theme`, `primary_keyword`, `primary_headline`, `primary_asset`, `chart_coin`, `sources_used`, `source_urls`, `combined_key_facts`, `aggregated_raw_content` (≥600 words).

**Workspace skills/scripts:**

| Path | Purpose |
|------|---------|
| `skills/research/verify_feeds.sh` | RSS health check |
| `skills/history/article_history.sh` | SQLite duplicate URL check (7-day window) — used inside HEADLINE_SCAN |
| `skills/web-reader-pro/SKILL.md` | Optional fallback reader — not primary path in SOUL |

**Duplicate guards (cross-cutting):**
1. Scout HEADLINE_SCAN — `article_history.sh check` per candidate URL (7-day TTL).
2. `build_picker_input.py` — filters URLs already in `picked_stories` with `status IN ('drafted','published')` (permanent dedup).
3. Nexus topic dedup — `check_recent_topic_duplicates.py` per pick (24h topic-similarity window vs `recent_topics.json`).

---

### Picker — Sieve

| Field | Value |
|-------|-------|
| Workspace | `workspace-picker/` |
| SOUL | `workspace-picker/SOUL.md` |
| Model | gpt-5.4 (or pro for stronger reasoning) |
| Pipeline step | 1c |

**Job:** Read `picker_input.json` (target_count, recent_categories, candidates[]), classify each candidate into exactly one of 8 categories, then select N picks balancing freshness with category diversity. Write `picks.json`.

**Category taxonomy (closed set):**

| Category id | Coverage |
|---|---|
| `regulation` | Lawmakers, SEC/CFTC/EU/FCA actions, rulings, sanctions |
| `etf_institutional` | Spot/futures ETFs, BlackRock/Fidelity, treasury allocations |
| `hack_exploit` | Bridge hacks, smart-contract exploits, breaches |
| `l1_l2_protocol` | Major upgrades/forks/launches on L1s and L2s |
| `exchange` | CEX/DEX product launches, listings, outages |
| `stablecoin` | USDT/USDC/DAI mints/burns/depegs/issuer + stablecoin-specific regulation |
| `adoption_partnership` | Corporate adoption, payment integrations, MoUs |
| `market_movement` | Catalyst-driven BTC/ETH/altcoin price action |

**Selection algorithm:** Per-candidate `selection_score` from `category_score`, `recency_score` (pub_date), `corroboration_score`, `source_priority_score`. Then greedy pick with `recent_categories` penalty (×0.55, ×0.45 for top recent) and `same_batch` penalty (×0.50 primary / ×0.85 alt overlap). Early-stop when remaining `slot_score < 0.30`.

**Tools denied:** `web_search`, `web_fetch` (Picker reads/writes JSON only — no external calls)

**Output artifact:** `$RUN_DIR/picker/picks.json` → `validate_picks.py` inserts each pick into `editorial.db`'s `picked_stories` table.

---

### Writer — Quill

| Field | Value |
|-------|-------|
| Workspace | `workspace-writer/` |
| SOUL | `workspace-writer/SOUL.md` |
| Template | `workspace-writer/COINOGRAPHY_TEMPLATE.md` |
| Model | gpt-5.4 |
| Pipeline step | 2 |

**Job:** Read `validated.json`, write SEO article per COINOGRAPHY rules, output to `$RUN_DIR/article/raw.md`, yield `SUCCESS`.

**Editorial rules (summary):** 1000–1200 body words; META limits (55/70/155 chars); exactly 2 source anchor links; 2–4 H2s, 3–6 H3s, 3–6 FAQs; fixed section order (Conclusion before FAQs).

**Tools:** Default coding profile (no special allow/deny).

**Validation scripts (run by Nexus, not Quill):**

| Script | Purpose |
|--------|---------|
| `skills/validate_article_structure.py` | H2/H3/FAQ counts, section order |
| `skills/validate_anchor_links.py` | Exactly 2 distinct source URLs |

**Output artifact:** `$RUN_DIR/article/raw.md` → synced to `final.md` at `/tmp/crypto-article.md`

---

### Chart Generator — Pixel (optional)

| Field | Value |
|-------|-------|
| Workspace | `workspace-chart-generator/` |
| SOUL | `workspace-chart-generator/SOUL.md` |
| Model | gpt-5.4-mini |
| Pipeline step | 2.5 (skipped when `ENABLE_ARTICLE_CHARTS=0`) |

**Job:** Parse `CHART_COIN` / `CHART_DAYS` / `CHART_OUTPUT` → CoinGecko chart script → return `CHART_DONE:` or `CHART_ERROR:`.

**Skill:** `skills/chart-generator/SKILL.md` + `skills/chart-generator/scripts/generate_chart.py`

**Output artifact:** `$RUN_DIR/media/chart.png` (symlink `/tmp/chart.png`)

---

### Creator — Pixel (image)

| Field | Value |
|-------|-------|
| Workspace | `workspace-creator/` |
| SOUL | `workspace-creator/SOUL.md` |
| Model | gpt-5.4-mini |
| Pipeline step | 3 |

**Job:** Craft editorial prompt (human + crypto asset + Reuters-style suffix) → run Imagen skill → verify JPEG.

**Skill:** `skills/generate-image/SKILL.md` + `skills/generate-image/generate.sh` (Vertex Imagen 4 via Bifrost, logo stamp)

**Success output:** `/tmp/crypto-feature.jpg`  
**Failure output:** `IMAGE_FAILED: <error log>`

---

### Publisher — Press

| Field | Value |
|-------|-------|
| Workspace | `workspace-publisher/` |
| SOUL | `workspace-publisher/SOUL.md` |
| Model | gpt-5.4-mini |
| Pipeline step | 4 |

**Job:** Nexus often pre-builds DOCX with pandoc; Press runs `gog drive upload` and returns `webViewLink`.

**Skill:** `skills/gog/SKILL.md` — Google Workspace CLI

**Rule:** No `--convert` on .docx uploads (strips embedded images).

---

### WordPress Publisher — Scribe

| Field | Value |
|-------|-------|
| Workspace | `workspace-wp-publisher/` |
| SOUL | `workspace-wp-publisher/SOUL.md` |
| Model | gpt-5.4-mini |
| Pipeline step | 6 (only after explicit user "yes") |

**Job:** Run `publish.sh` → read `/tmp/wp-result.txt` or `/tmp/wp-error.log`.

**Skills:**

| Path | Purpose |
|------|---------|
| `skills/wordpress/SKILL.md` | Publish workflow docs |
| `skills/wordpress/publish.sh` | META parse, Gutenberg blocks, Rank Math SEO, feature image (draft default) |
| `skills/wordpress/wp_post_actions.sh` | Telegram Publish/Unpublish/Edit; `--author` on publish |
| `skills/wordpress/html_to_gutenberg.py` | Markdown HTML → Gutenberg blocks |
| `skills/history/article_history.sh` | History helper |

**Success output:** WordPress post URL  
**Failure output:** `WP_FAILED: <reason>`

---

### Main (not in pipeline)

| Field | Value |
|-------|-------|
| Workspace | `workspace/` |
| Model | gpt-5.4-mini |
| Role | Default general OpenClaw workspace |

**Extra skills:** `content-writer`, `programmatic-seo`, `web-reader-pro`, `gog`

---

## Orchestrator pipeline scripts

All under `workspace-orchestrator/skills/pipeline/`:

| Script | Function |
|--------|----------|
| `init_run.sh` | Create run bundle, manifest (now includes `batch.target_count` + `pick_run_id`), `/tmp` symlinks, env file. Touches `headlines.json`, `picker_input.json`, `picks.json` placeholders too. |
| `manifest_paths.py` | Resolve artifact paths from manifest |
| `validate_headlines.py` | NEW — validate Scout's `HEADLINE_SCAN` output, dedupe URLs, normalize pub_date |
| `build_picker_input.py` | NEW — assemble picker input with `target_count`, `recent_categories`, and consumed-URL filter from `picked_stories` |
| `validate_picks.py` | NEW — validate Sieve's `picks.json`, enforce taxonomy, insert each pick into `picked_stories` |
| `update_pick_status.py` | NEW — CLI wrapper around `editorial_db.update_pick_status` (researching/writing/drafted/published/failed/cancelled) |
| `switch_iteration.sh` | NEW — `--start <N>` archives previous iter into `iter_<N-1>/` and truncates canonical files; `--archive <N>` saves last iter; `--reset <N>` archives into `iter_<N>_failed/` after a mid-iteration failure |
| `validate_research.py` | Validate raw research JSON → `validated.json`. Now accepts `--raw-path` + `--validated-path` for per-iteration variants and carries `category` into manifest.story. |
| `check_recent_topic_duplicates.py` | 24h topic dedup vs `state/recent_topics.json` |
| `update_recent_topics.py` | Register researched/drafted/published topics. Now also stores `category`. |
| `verify_artifacts.py` | Stage gates: `pre_write`, `pre_sync`, `post_sync`, `post_image` (feature_image ≥50 KB, JPEG magic bytes), `pre_drive`, `pre_wp`. `post_image` is run after Creator (Pixel) yields; retried once with Universal Fallback prompt before continuing without image. |
| `sync_article_from_raw.py` | Sanitize raw.md → final.md; word count + topic gate |
| `count_article_body_words.py` | Body word count (same logic as sync; writer pre-flight) |
| `update_manifest_step.sh` | Record step status in manifest |
| `cleanup_run_artifacts.sh` | Remove `/tmp` symlinks on terminal state — runs ONCE per batch |
| `save_google_drive_json.py` | Persist `publish/google-drive.json` after Step 2.4 |
| `build_and_send_card.py` | Step 2.6 — Telegram news card + `editorial.db`. Now also writes `category` into `articles`. |
| `handle_card_feedback.py` | RATE, Publish (author picker), Unpublish, Edit |
| `editorial_db.py` | SQLite store. Now defines `picked_stories` + `articles.category`, plus helpers `insert_picked_stories`, `update_pick_status`, `recent_published_categories`, `pending_picks_for_run`, `get_pick`, `get_pick_by_index`, `list_picks_by_run` |

**Editorial config:** `workspace-orchestrator/config/wp_authors.json` (Toby 3, Ahmed 17, Golan 8), `telegram_card_config.json`

---

## Skills inventory

### Pipeline-critical (workspace-specific)

| Agent | Skill / scripts |
|-------|-----------------|
| Orchestrator | Pipeline scripts (14 files above) + `EDITORIAL_FEEDBACK.md` |
| Researcher | `verify_feeds.sh`, `article_history.sh`, `web-reader-pro` |
| Writer | `validate_article_structure.py`, `validate_anchor_links.py` |
| Chart-generator | `skills/chart-generator/` (global) |
| Creator | `generate-image/` |
| Publisher | `gog/` |
| WP-publisher | `wordpress/publish.sh`, `wordpress/wp_post_actions.sh`, `article_history.sh` |

### Global OpenClaw skills (visible to agents)

`chart-generator`, `clawhub`, `coding-agent`, `gog`, `healthcheck`, `mcporter`, `node-connect`, `skill-creator`, `taskflow`, `taskflow-inbox-triage`, `tmux`, `video-frames`, `weather`

Researcher additionally has workspace `web-reader-pro`.

---

## Run-bundle artifact model

Each pipeline run creates an isolated folder. Multi-story batches reuse the canonical paths across iterations and archive each completed iteration into `iter_<N>/` via `switch_iteration.sh`:

```
/tmp/crypto-run-<RUN_ID>/
├── manifest.json
├── .run_started               (epoch stamp; refreshed at start of each iter)
├── research/
│   ├── headlines.json         (HEADLINE_SCAN output, batch-level)
│   ├── raw.json               (DEEP_RESEARCH output for current iter)
│   └── validated.json         (validate_research.py output for current iter)
├── picker/
│   ├── picker_input.json
│   └── picks.json
├── article/
│   ├── raw.md                 (current iter)
│   ├── final.md               (current iter)
│   ├── with-image.md
│   └── article.docx
├── media/
│   ├── chart.png              (optional)
│   └── feature.jpg
├── publish/
│   ├── google-drive.json
│   ├── wordpress.json
│   └── news-card.json
├── iter_1/                    (archive — populated at start of iter 2 or via --archive 1)
│   ├── research/, article/, media/, publish/
│   └── manifest.snapshot.json
├── iter_2/                    (archive of iter 2)
└── iter_<K>_failed/           (only if iteration failed; contains REASON.txt)
```

Legacy `/tmp/...` paths are symlinks into the canonical (current-iteration) files; they never need to change between iterations because canonical paths are reused.

**DB-backed batch state:** `~/.openclaw/data/editorial.db` → `picked_stories` rows for `pick_run_id = "<RUN_ID>-pick"` track per-pick status (`pending`/`researching`/`writing`/`drafted`/`published`/`failed`/`cancelled`). The orchestrator can resume mid-batch by querying `pending_picks_for_run`.

---

## Known gaps and legacy docs

| Item | Notes |
|------|-------|
| Two "Pixel" personas | `creator` (Imagen feature images) vs `chart-generator` (CoinGecko charts) |
| Charts off by default | `ENABLE_ARTICLE_CHARTS=0` in Step 0 |
| Word count asymmetry | Writer told 1200 max; orchestrator sync silently accepts up to 1300 |
| Publisher SOUL vs Nexus | Press SOUL describes full pandoc flow; Nexus pre-builds DOCX and must run `save_google_drive_json.py` |
| Telegram vs DM | Pipeline gate in DM; news cards + editorial in `news-agent` group |
| Missing script | Docs reference `extract_tweet_quotes.py` under researcher — not present |
| `PIPELINE_DOCS/03_AGENT_PROFILES.md` | **Deprecated** — describes old `rm sessions.json` + `openclaw agent` flow |
| `PIPELINE_ARCHITECTURE.md` | High-level overview; see this registry for current agent/tool details |

---

## Related documentation

| File | Purpose |
|------|---------|
| **`AGENT_PIPELINE_REGISTRY.md`** | **This file — canonical living reference** |
| `PIPELINE_ARCHITECTURE.md` | High-level architecture overview |
| `PIPELINE_DOCS/` | Planning and legacy docs (some stale) |

---

## Change log

| Date | Change |
|------|--------|
| 2026-05-23 | Initial registry created; replaces `MULTI_AGENT_SYSTEM_DOCUMENTATION.md` |
| 2026-05-23 | Added `PLANS/tweet-embed-duckduckgo.md` — deferred tweet embed spec (not implemented) |
| 2026-05-25 | Writer length reliability: `count_article_body_words.py`; removed `pick_article_structure.py`; SOUL repair policy (measured length, REVISION MODE for structure/anchor after sync) |
| 2026-05-26 | Creator image provider: Leonardo → Vertex Imagen 4 via Bifrost (`generate.sh`) |
| 2026-05-26 | WordPress target: `https://coinography.com` (category 17) |
| 2026-05-26 | Step 6 default: WordPress draft (not live); Telegram card Publish promotes to live |
| 2026-05-27 | `wp_post_actions.sh` synced to coinography.com; Telegram publish author picker (Toby/Ahmed); `save_google_drive_json.py`; editorial `wp_status` defaults draft |
| 2026-06-03 | Multi-story Picker pipeline: new `picker` agent (Sieve), two-mode researcher (HEADLINE_SCAN / DEEP_RESEARCH), `picked_stories` table + `articles.category` column, per-iteration `iter_<N>/` archives via `switch_iteration.sh`, new scripts (`validate_headlines.py`, `build_picker_input.py`, `validate_picks.py`, `update_pick_status.py`), orchestrator SOUL rewritten with Step 0.5 (parse N) + Step 1a/1b/1c + per-pick loop with yes/no/stop user gate. |
| 2026-06-04 | Step 2.3 (Creator / Pixel) restored with concrete spawn recipe — was a stub referencing deleted legacy SOUL logic, causing creator to be silently skipped on all recent runs. Added `post_image` stage to `verify_artifacts.py` (size ≥ 50 KB, JPEG magic bytes, path under RUN_DIR) so future regressions fail loudly. Orchestrator retries Pixel once with Universal Fallback prompt before continuing without image. |
