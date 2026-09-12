# OpenClaw Crypto News Pipeline Architecture

> **For current agent, tool, and skill details see [AGENT_PIPELINE_REGISTRY.md](AGENT_PIPELINE_REGISTRY.md).**

## 1) System Overview

This project implements a multi-agent pipeline that produces a daily crypto news article and optionally publishes it to WordPress. The system is orchestrated by a single controller agent that sequences four worker agents in strict order, plus an optional fifth agent for WordPress publishing.

Primary flow:
Orchestrator -> Researcher -> Writer -> Image Creator -> (Drive upload, run in orchestrator bash)
Optional: WordPress publish (run in orchestrator bash)

> **Note (2026-06-19):** Google Drive upload and WordPress publishing are no longer separate LLM agents. They are deterministic shell scripts (`gog drive upload`, `publish.sh`) that the Orchestrator now runs directly in `bash` to save tokens. The `publisher`/`wp-publisher` workspaces remain as command references only. See `AGENT_PIPELINE_REGISTRY.md` for current details.

Goal:
Produce a structured, SEO-optimized crypto article, generate a Reuters-style feature image, upload a .docx to Google Drive, and optionally publish a live WordPress post.

---

## 2) Agent Roles and Responsibilities

### 2.1 Orchestrator: Nexus
Defined in:
- workspace-orchestrator/IDENTITY.md
- workspace-orchestrator/SOUL.md

Role: Pipeline controller. Only job is to run each step in order and hand off outputs.
Key responsibilities:
- Reset each agent session before calling it.
- Execute command sequences exactly as written.
- Validate output and handle failure recovery.
- Never fabricate URLs or results.

### 2.2 Researcher: Scout
Defined in:
- workspace-researcher/IDENTITY.md
- workspace-researcher/SOUL.md

Role: Crypto news researcher.
Key responsibilities:
- Fetch multiple RSS feeds (CoinDesk, CoinTelegraph, Decrypt, Google News).
- Identify a single topic covered by multiple sources, within the last 24 hours.
- Extract facts and provide aggregated JSON.

Output: RESEARCH_JSON with:
- topic_theme
- primary_keyword
- primary_headline
- sources_used
- source_urls
- combined_key_facts
- aggregated_raw_content (>= 600 words)

### 2.3 Writer: Quill
Defined in:
- workspace-writer/IDENTITY.md
- workspace-writer/SOUL.md
- workspace-writer/COINOGRAPHY_TEMPLATE.md

Role: Article writer.
Key responsibilities:
- Read the required template file.
- Produce a 1100-1200 word article strictly following the template.
- Embed up to **2** distinct source URL links in the body (hook or first H2); optional 1 tweet link separate.
- Include required sections, headings, and word count line.

Output:
- /tmp/crypto-article.md (after thinking blocks removed)

### 2.4 Image Creator: Pixel
Defined in:
- workspace-creator/IDENTITY.md
- workspace-creator/SOUL.md
- workspace-creator/skills/generate-image/SKILL.md

Role: Feature image generator.
Key responsibilities:
- Craft a Reuters-style editorial photo prompt from article metadata.
- Run a hardened bash script that calls Leonardo AI.
- Return the real saved file path or a failure string.

Output:
- Success: /tmp/crypto-feature.jpg
- Failure: IMAGE_FAILED: <reason>

### 2.5 Google Drive upload (INLINED — orchestrator bash, formerly "Press")
Defined in:
- workspace-orchestrator/SOUL.md (Step 2.4)
- workspace-publisher/SOUL.md (reference only — agent no longer spawned)
- workspace-publisher/skills/gog/SKILL.md

Role: Uploads the article to Google Drive as .docx. Run directly by the orchestrator (no LLM subagent).
Key responsibilities:
- Embed image at top of markdown (if available).
- Convert to docx using pandoc.
- Upload using gog CLI and return webViewLink.

Output:
- Google Drive webViewLink from JSON response.

### 2.6 WordPress publish (INLINED — orchestrator bash, formerly "Scribe", Optional)
Defined in:
- workspace-orchestrator/SOUL.md (Step 2.6)
- workspace-wp-publisher/SOUL.md (reference only — agent no longer spawned)
- workspace-wp-publisher/skills/wordpress/SKILL.md

Role: Publishes the article to WordPress as a draft (run directly by the orchestrator via `publish.sh`; no LLM subagent). Telegram card Publish button promotes a draft to live.
Key responsibilities:
- Run a bash script to upload image, convert markdown to HTML, and create a post (`status: publish`).
- Return the live post URL from the output file.
- Report exact errors on failure.

Output:
- Success: WordPress post URL
- Failure: WP_FAILED: <reason>

---

## 3) Artifact Isolation — Run-Bundle Model

Every pipeline execution creates one isolated run-bundle under `/tmp/crypto-run-<RUN_ID>/`. All artifacts for that run live inside it. Legacy `/tmp/...` paths are symlinks into the active bundle so existing scripts keep working.

**Lineage rule:**

| Stage | Reads | Writes |
|-------|-------|--------|
| Scout | RSS feeds | `research/raw.json` |
| validate_research.py | raw.json | `research/validated.json` |
| Quill | validated.json | `article/raw.md` |
| sync_article_from_raw.py | raw.md + validated | `article/final.md` |
| pandoc | with-image.md | `article/article.docx` |
| WordPress / Drive | article_final / docx | `publish/*.json` |

**Coherence gates (fail = pipeline stops):**

| Gate | Triggered | Checks |
|------|-----------|--------|
| `pre_write` | After research | research_validated exists + non-empty |
| `pre_sync` | After writer | article/raw.md fresh (mtime ≥ `.run_started`) |
| `post_sync` | After sync | final.md fresh; H1 matches headline; ≥1 source URL matches research |
| `pre_drive` | Before Drive | docx newer than final.md; paths under active RUN_DIR |
| `pre_wp` | Before WP | All pre_drive + repeated H1/source coherence |

**Cleanup:** `cleanup_run_artifacts.sh` runs only on terminal state (user NO to WP, WP success, or fatal failure). `$RUN_DIR` is never deleted automatically (7-day prune on next run start).

---

## 4) Pipeline Execution Flow

### Step 1: Research
Orchestrator clears researcher session, calls researcher agent.
Result: RESEARCH_JSON

### Step 2: Write Article
Orchestrator clears writer session, calls writer.
Writer reads template and writes article.
Orchestrator removes thinking blocks and validates word count and completion.
Result: /tmp/crypto-article.md

### Step 3: Generate Image
Orchestrator extracts image inputs from RESEARCH_JSON.
Calls creator to generate feature image using skill script.
Result: /tmp/crypto-feature.jpg or IMAGE_FAILED

### Step 4: Publish to Google Drive
If image exists:
- prepend markdown image
- convert to docx
If image failed:
- upload markdown without image
Result: Google Drive URL

### Step 5: Confirmation Gate
Orchestrator returns:
- Article title
- Google Drive URL
- Image path
Then waits for explicit user confirmation.

### Step 6: Publish to WordPress (Optional)
If user says yes:
- Run WordPress publishing script
- Return draft URL
If user says no:
- End pipeline

---

## 4) Inputs, Outputs, and Artifacts

Inputs:
- RSS feeds and web pages (Researcher)
- COINOGRAPHY_TEMPLATE.md (Writer)
- RESEARCH_JSON (Writer and Creator)

Outputs:
- RESEARCH_JSON (structured facts)
- /tmp/crypto-article.md (markdown article)
- /tmp/crypto-feature.jpg (feature image)
- /tmp/crypto-article.docx (converted article)
- Google Drive webViewLink
- WordPress draft URL (optional)

---

## 5) Tooling and Skills

### Image Generation Skill
Defined in:
- workspace-creator/skills/generate-image/SKILL.md

Functionality:
- Calls Leonardo AI API
- Handles retries and validation
- Saves JPEG to /tmp/crypto-feature.jpg

### Google Drive Upload (gog CLI)
Defined in:
- workspace-publisher/skills/gog/SKILL.md

Functionality:
- Uploads docx
- Returns JSON with webViewLink

### WordPress Publish Skill
Defined in:
- workspace-wp-publisher/skills/wordpress/SKILL.md

Functionality:
- Uploads image
- Converts markdown to HTML
- Creates draft post

---

## 6) Reliability and Failure Handling

Researcher:
- If no multi-source topic found, selects single best story from CoinTelegraph.

Writer:
- If word count missing, Orchestrator forces a retry.
- If too long, Orchestrator requests summarization pass.

Creator:
- If Leonardo script fails, returns IMAGE_FAILED.
- Orchestrator proceeds without image if needed.

Publisher:
- If upload fails, returns exact error.

WordPress:
- If publishing fails, returns WP_FAILED.

---

## 7) Security and Credentials

Credentials exist in environment-specific files and scripts.
This doc intentionally omits or redacts sensitive values.
See these for configuration (do not share publicly):
- workspace-creator/TOOLS.md
- workspace-wp-publisher/TOOLS.md

---

## 8) Directory Map (Key Files)

Orchestrator:
- workspace-orchestrator/SOUL.md

Researcher:
- workspace-researcher/SOUL.md
- workspace-researcher/skills/web-reader-pro/SKILL.md

Writer:
- workspace-writer/SOUL.md
- workspace-writer/COINOGRAPHY_TEMPLATE.md

Creator:
- workspace-creator/SOUL.md
- workspace-creator/skills/generate-image/SKILL.md

Publisher:
- workspace-publisher/SOUL.md

WordPress Publisher:
- workspace-wp-publisher/SOUL.md

---

## 9) Operational Notes

- Each agent session is cleared before each run to avoid state bleed.
- The Orchestrator enforces strict ordering and validates outputs.
- WordPress publishing is gated by explicit user confirmation.
- No agent may fabricate URLs or success outputs.

---

## 10) Extension Points

Possible future additions:
- Add more research sources with quality scoring.
- Add a summarizer fallback if writer exceeds word limit.
- Add image alternatives for failures (stock images).
- Add a notification step (email or Slack) after publishing.

---

## 11) Quick Reference: Pipeline Summary

1) Research topic and facts
2) Write article from template
3) Generate feature image
4) Convert to docx and upload
5) Ask for WordPress confirmation
6) Publish draft if confirmed
