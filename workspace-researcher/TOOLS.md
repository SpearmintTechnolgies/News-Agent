# TOOLS.md — Scout's local notes

Skills define _how_ tools work. This file is for researcher-specific reference. Procedures live in `skills/headline-scan/`, `skills/deep-research/`, and `skills/research-check/`.

## RSS sources

Feeds are **project-driven** — the orchestrator sets `PROJECT_CONFIG` and the feed list comes from `research.rss_feeds`. Do not hardcode URLs. Fetch them with:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/emit_feed_fetch_commands.py \
  --output-dir /tmp/feeds > /tmp/feeds/fetch.sh && bash /tmp/feeds/fetch.sh
```

Verify the active project's feeds anytime:

```bash
bash ~/.openclaw/workspace-researcher/skills/research/verify_feeds.sh [--project <slug>]
```

**HEADLINE_SCAN primary path** (deterministic — run this first):

```bash
OUTPUT_FILE="<path>" TARGET_COUNT=10 \
  python3 ~/.openclaw/workspace-researcher/skills/headline-scan/scan_headlines.py
```

**DEEP_RESEARCH extraction** (multi-tier fallbacks):

```bash
python3 ~/.openclaw/workspace-researcher/skills/deep-research/extract_article.py "<publisher_url>"
```

Selection policy and exclusions also come from the project config (`research.exclude_keywords`, `research.source_priority_order`). See [`skills/headline-scan/SKILL.md`](skills/headline-scan/SKILL.md).

## Aggregator URL resolver

Resolve `news.google.com` / `/rss/articles/` wrappers to the real publisher URL before using them:

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/resolve_url.py "<url>"
```

Prints the publisher URL, or `RESOLVE_FAILED`. See [`skills/research-check/SKILL.md`](skills/research-check/SKILL.md).

## Article history tool

Check a candidate URL before researching it, to avoid duplicates:

```bash
bash ~/.openclaw/workspace-researcher/skills/history/article_history.sh check "https://coindesk.com/example"
```

- `EXISTS` — published within the last 7 days; drop it.
- `NOT_FOUND` — safe to use.

## Self-check validator

Always self-check the output before yielding `SUCCESS`:

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
  --file "$OUTPUT_FILE" --mode deep_research|headline_scan
```
