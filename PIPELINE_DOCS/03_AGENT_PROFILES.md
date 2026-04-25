# Agent Profiles

This document details the exact role, logic, and limitations of each of the 5 agents in the pipeline. Each agent is defined by its `SOUL.md` file located in its respective workspace.

---

## 1. Nexus (The Orchestrator)
- **Path:** `~/.openclaw/workspace-orchestrator/SOUL.md`
- **Role:** The Manager.
- **Logic:**
  1. It contains the exact bash commands required to spawn the other agents.
  2. **Crucial Mechanic:** Before it spawns an agent, it runs `rm -f ~/.openclaw/agents/[agent_name]/sessions/sessions.json`. This forcibly wipes the sub-agent's memory so it doesn't hallucinate context from previous runs.
  3. It explicitly parses the final JSON output from the Publisher to prevent hallucinating the Google Drive URL.

## 2. Scout (The Researcher)
- **Path:** `~/.openclaw/workspace-researcher/SOUL.md`
- **Role:** Data Gatherer.
- **Logic:**
  1. Uses `curl` to fetch RSS feeds from CoinTelegraph, CoinDesk, and Decrypt.
  2. Analyzes the XML to find the **Single Biggest News Event** currently happening across multiple sources.
  3. Uses a fallback `curl | sed` command to scrape the raw HTML body of the articles.
  4. Formats everything into a highly structured JSON payload (`topic_theme`, `primary_headline`, `combined_key_facts`) and passes it up the chain.

## 3. Quill (The Writer)
- **Path:** `~/.openclaw/workspace-writer/SOUL.md`
- **Role:** SEO Crypto Journalist.
- **Logic:**
  1. Accepts the JSON payload from Scout.
  2. Follows a **Strict Markdown Template**: Hook → Section 1 → Section 2 → Section 3 → Section 4 → Conclusion → FAQs.
  3. Enforces SEO rules: primary keywords in the H1 and opening paragraph, exactly 150-character meta descriptions, and zero "fluff" adjectives.
  4. Saves the output to `/tmp/crypto-article.md`.

## 4. Pixel (The Creator)
- **Path:** `~/.openclaw/workspace-creator/SOUL.md`
- **Role:** Visual Artist.
- **Logic:**
  1. Accepts the article title from Nexus.
  2. Executes a strict 3-step `curl` chain against the **Leonardo AI API**:
     - `POST` to start generation.
     - `sleep 15` then `GET` to check status and extract the URL.
     - `curl -o` to download the image to `/tmp/crypto-feature.jpg`.
  3. Contains auto-retry logic for 500 errors and graceful degradation for Billing Errors (returns `IMAGE_FAILED` so the pipeline continues without crashing).

## 5. Press (The Publisher)
- **Path:** `~/.openclaw/workspace-publisher/SOUL.md`
- **Role:** Distribution & Conversion.
- **Logic:**
  1. Usually triggered *after* Nexus has used `pandoc` to merge the Markdown and the JPG into `/tmp/crypto-article.docx`.
  2. Uses the `gog` CLI tool to upload the document.
  3. **Crucial Mechanic:** It explicitly does **NOT** use the `--convert` flag when uploading the `.docx`. If `--convert` is used, Google's import API strips out the AI-generated image. By uploading it natively as a `.docx`, the image is preserved.
  4. Passes the `webViewLink` JSON back to Nexus.
