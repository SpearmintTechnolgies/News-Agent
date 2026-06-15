# Skill: deep-research

Use when the spawn message starts with `MODE: DEEP_RESEARCH` (also the default if no `MODE:` line is present). Do a full deep dive on ONE story the Picker already chose. You do **not** re-pick.

## Spawn message

```
MODE: DEEP_RESEARCH
INPUT_FILE: <absolute path to picks.json>
PICK_INDEX: <integer, 1-based>
OUTPUT_FILE: <absolute path to write raw.json for this iteration>
```

## Step 1 — Read the assigned pick

Read `INPUT_FILE` (a JSON with a `picks` array). Find the entry whose `pick_index == PICK_INDEX` and extract: `headline`, `url`, `category`, `wp_category_slugs`, `wp_category_ids`, `corroborating_sources`.

If the entry is missing or the index is out of range, write `{"status":"error","reason":"pick_index_not_found"}` to `OUTPUT_FILE` and go to Step 6.

## Step 2 — Resolve, then validate URLs

**Resolve aggregator wrappers first** (uses disk cache + retry in `resolve_url.py`):

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/resolve_url.py "<url>"
```

Use the printed publisher URL as the scrape target. If `RESOLVE_FAILED` for the primary, prefer a non-aggregator corroborating URL. If only unresolvable wrappers remain, write `{"status":"error","reason":"url_unresolvable"}` and go to Step 6.

Build a target list of **2–3 URLs** when available:
1. Resolved primary URL (required)
2. Up to 2 resolved corroborating URLs from `corroborating_sources`

**Minimum 2 sources:** when corroborating URLs exist, you must extract at least **2** distinct publisher URLs. Only fall back to 1 source if every corroborating URL fails resolve/liveness/extraction.

Check liveness per URL:

```bash
curl -o /dev/null -s -w "%{http_code}\n" "<resolved_url>"
```

If the primary returns 404/410, promote a corroborating URL. If all URLs are dead, write `{"status":"error","reason":"all_urls_dead"}` and go to Step 6.

## Step 3 — Deep extraction (parallel, multi-source)

Do **not** use the internal `web_fetch` tool on native publisher URLs (Cloudflare blocks it).

Use the tested extraction ladder via `extract_article.py` for **each** resolved URL. Run extractions **in parallel** when you have 2+ URLs (separate exec calls in one turn, or background jobs):

```bash
python3 ~/.openclaw/workspace-researcher/skills/deep-research/extract_article.py "<resolved_url>"
```

`extract_article.py` tries, in order: trafilatura → curl+trafilatura → bs4/selectolax → (optional) lynx → (optional) markdownify WebFetch. It prints `EXTRACT_OK: tier=… words=…` on stderr.

Aggregate clean prose from all successful extractions into one string (target **600+ words** total). Never store page HTML or tool logs — only clean article prose.

If fewer than 2 sources extracted but corroboration was available, try remaining corroborating URLs before failing.

## Step 4 — Build the research JSON

Write to `OUTPUT_FILE`:

```json
{
  "status": "ok",
  "mode": "deep_research",
  "story_id": "short-slug-identifying-this-story",
  "category": "<primary WP slug from picks.json — copy through unchanged>",
  "wp_category_slugs": ["<copy through from picks.json>"],
  "wp_category_ids": [0],
  "topic_theme": "The core topic (e.g., Bitcoin ETF Inflows Surge)",
  "primary_keyword": "A 2-3 word SEO keyword",
  "primary_headline": "Best descriptive headline (the picked one or refined)",
  "primary_asset": "Primary crypto asset (Bitcoin, Ethereum, …)",
  "chart_coin": "CoinGecko coin id for the primary asset",
  "sources_used": ["CoinTelegraph", "CoinDesk"],
  "source_urls": ["https://…/primary", "https://…/corroborating"],
  "combined_key_facts": [
    "Specific data point or quote from source A",
    "Specific data point or context from source B"
  ],
  "aggregated_raw_content": "Combined clean article text from all sources. 600+ words."
}
```

- `category`, `wp_category_slugs`, `wp_category_ids` are **required** — copy through from `picks.json` exactly.
- `source_urls` must be **resolved publisher URLs**, never `news.google.com` wrappers.
- When corroboration was available, `source_urls` should list **≥2** entries.
- `combined_key_facts` must have **≥2** entries drawn from distinct sources when 2+ sources were extracted.
- Never fabricate data, quotes, or URLs. Aggregate ONE story only.

## Step 5 — Final verification thinking block

Before self-check, use a short `<thinking>` block:

- `source_urls` has no aggregator wrappers.
- If corroboration existed: at least 2 URLs extracted (or document why only 1 was possible).
- `aggregated_raw_content` is clean prose, not HTML/logs.
- `combined_key_facts` has ≥2 items when multi-source.

Repair or re-extract if needed. Do not write thinking into `OUTPUT_FILE`.

## Step 6 — Self-check then yield

Run the self-check loop in [`../research-check/SKILL.md`](../research-check/SKILL.md):

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
  --file "$OUTPUT_FILE" --mode deep_research
```

Yield `SUCCESS` only on `RESEARCH_CHECK: PASS` (max 3 self-iterations). A clean error JSON (`status:error` with a known reason) passes the check — yield `SUCCESS` so the orchestrator skips the story cleanly.

**Hard rule:** if you cannot extract clean article text, write the structured error JSON above. **Never** write logs, raw HTML, or scrape dumps into `OUTPUT_FILE`.
