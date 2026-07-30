# Skill: headline-scan

Use when the spawn message starts with `MODE: HEADLINE_SCAN`. Collect ~10 fresh, distinct candidate headlines from the project RSS feeds for the Picker. **No deep extraction** — do not open article pages, do not call `trafilatura`/`lynx`/`web-reader-pro`.

## Spawn message

```
MODE: HEADLINE_SCAN
OUTPUT_FILE: <absolute path to headlines.json>
TARGET_COUNT: <integer, default 10>
```

If `TARGET_COUNT` is missing, use 10. The orchestrator also sets `PROJECT_CONFIG` (path to `projects/<slug>.json`); feeds and editorial filters are project-driven (`research.*`).

## Step 1 — Deterministic scan (primary path)

Run the project-aware scanner first. It parallel-fetches feeds, resolves Google News URLs, applies `exclude_keywords`, dedupes, and batch-checks history in one pass.

```bash
OUTPUT_FILE="<OUTPUT_FILE>" TARGET_COUNT="<TARGET_COUNT>" \
  python3 ~/.openclaw/workspace-researcher/skills/headline-scan/scan_headlines.py
```

**Success criteria:** exit code `0` **and** `candidate_count >= min(3, TARGET_COUNT)` in the written JSON.

If both pass, skip to **Step 4 (Final verification)** then **Step 5 (Self-check)**.

## Step 2 — LLM fallback (only if Step 1 fails)

Use this path when the scanner exits non-zero, throws, or returns too few candidates (network hiccup, all feeds down, script error). Do **not** skip this — reliability over speed.

### 2a — Fetch all feeds

```bash
rm -rf /tmp/feeds && mkdir -p /tmp/feeds
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/emit_feed_fetch_commands.py \
  --output-dir /tmp/feeds > /tmp/feeds/fetch.sh
bash /tmp/feeds/fetch.sh
ls -la /tmp/feeds/*.xml | head
```

Each `/tmp/feeds/<NN>.source` holds the friendly source name. Pair each `.xml` with its `.source`. 12s timeout; if a feed is 403/empty, continue with the others.

### 2b — Parse items

Per `<item>` extract:
- `headline` — `<title>` (strip HTML, decode entities, trim)
- `url` — `<link>`
- `pub_date` — `<pubDate>` (ISO 8601 if possible)
- `source` — friendly source name
- `summary` — `<description>`, first 280 chars, plain text

**Resolve aggregator wrappers:**

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/resolve_url.py "<url>"
```

Use the printed publisher URL. If it prints `RESOLVE_FAILED`, drop that candidate.

**Hard filters before keeping an item:**
1. `pub_date` within `research.max_age_hours` (default 24h). If unparseable, keep only if among the first 5 items of that feed.
2. Drop headlines matching the project `research.exclude_keywords`.
3. Drop `mailto:` / `javascript:` / empty / unresolved-aggregator URLs.

### 2c — Deduplicate

- **URL dedupe:** same URL -> keep the first.
- **Same-story dedupe:** near-identical headlines (>=80% significant-token overlap) -> keep the higher-priority source per `research.source_priority_order`. Record up to 5 dropped duplicates as `corroborating_sources` (`{source, url}`).

### 2d — History gate

Batch when possible:

```bash
python3 ~/.openclaw/workspace-researcher/skills/history/history_batch.py check-batch --project "$PROJECT_SLUG" --stdin <<< "$(printf '%s\n' <urls>)"
```

Or per URL:

```bash
bash ~/.openclaw/workspace-researcher/skills/history/article_history.sh check "<candidate_url>"
```

`NOT_FOUND` -> keep. `EXISTS` -> drop.

### 2e — Trim and write

Keep at most `TARGET_COUNT` candidates, ordered by `pub_date` desc. Write to `OUTPUT_FILE` (schema below).

If no usable candidates survive, write `{"status":"error","reason":"no_candidates"}` instead.

## Step 3 — (skipped when Step 1 succeeds)

Steps 2a–2e are the manual fallback only.

## Step 4 — Final verification thinking block

**Run this only once, at the end, before self-check.** Use a short `<thinking>` block:

- JSON at `OUTPUT_FILE` is parseable and `status` is `ok` or a clean `error`.
- If `ok`: `candidate_count >= min(3, TARGET_COUNT)` (or explain why fewer is acceptable).
- No candidate `url` contains `news.google.com`.
- Headlines look on-topic for the active project (memecoin vs general crypto).
- If any check fails: repair the JSON or re-run Step 2 fallback for missing candidates.

Do **not** dump thinking into `OUTPUT_FILE`.

## Step 5 — Self-check then yield

Run the self-check loop in [`../research-check/SKILL.md`](../research-check/SKILL.md):

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
  --file "$OUTPUT_FILE" --mode headline_scan
```

Yield `SUCCESS` only on `RESEARCH_CHECK: PASS` (max 3 self-iterations). Never write a log or HTML dump into `OUTPUT_FILE`.

## Output schema

```json
{
  "status": "ok",
  "mode": "headline_scan",
  "scanned_at": "<ISO 8601 UTC>",
  "target_count": 10,
  "candidate_count": 10,
  "candidates": [
    {
      "candidate_index": 1,
      "headline": "…",
      "url": "https://publisher.example/article",
      "pub_date": "2026-06-03T11:42:00Z",
      "source": "CoinDesk",
      "summary": "Up to 280 chars…",
      "corroborating_sources": [{ "source": "CoinTelegraph", "url": "https://…" }]
    }
  ]
}
```
