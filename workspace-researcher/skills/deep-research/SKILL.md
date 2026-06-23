# Skill: deep-research

Use when the spawn message starts with `MODE: DEEP_RESEARCH` (also the default if no `MODE:` line is present). Do a full deep dive on ONE story the Picker already chose. You do **not** re-pick.

## Spawn message

```
MODE: DEEP_RESEARCH
INPUT_FILE: <absolute path to picks.json>
PICK_INDEX: <integer, 1-based>
OUTPUT_FILE: <absolute path to write raw.json for this iteration>
```

## Step 1 — Run deterministic extractor (REQUIRED FIRST)

Do **not** manually orchestrate resolve/curl/extract until Step 3 fallback.

```bash
python3 ~/.openclaw/workspace-researcher/skills/deep-research/run_deep_research.py \
  --input "$INPUT_FILE" \
  --pick-index $PICK_INDEX \
  --output "$OUTPUT_FILE"
```

The script resolves URLs, parallel-extracts, discovers corroborating sources from the headline pool and RSS when content is thin, and assembles `raw.json`. Zero LLM tokens.

- Exit **0** → proceed to Step 2.
- Exit **1** with `DEEP_RESEARCH_PARTIAL` on stderr → proceed to Step 3 (do not write clean error JSON yet).

## Step 2 — Self-check then yield

Run the self-check loop in [`../research-check/SKILL.md`](../research-check/SKILL.md):

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
  --file "$OUTPUT_FILE" --mode deep_research
```

If **`RESEARCH_CHECK: PASS`**, yield `SUCCESS`.

## Step 3 — Fallback (only if Step 1 exit 1 OR Step 2 FAIL)

Re-run the extractor with aggressive discovery (max **2** re-runs per spawn):

```bash
python3 ~/.openclaw/workspace-researcher/skills/deep-research/run_deep_research.py \
  --input "$INPUT_FILE" \
  --pick-index $PICK_INDEX \
  --output "$OUTPUT_FILE" \
  --discover-aggressive
```

Then re-run Step 2 self-check.

**Hard rules:**
- If `partial_words > 0` in the output file, **never** write clean error JSON — keep re-running with `--discover-aggressive`.
- Only write clean error JSON (`pick_index_not_found`, `url_unresolvable`, `all_urls_dead`) when **zero** words were extracted across all URLs.
- Never fabricate data, quotes, or URLs.

## Expected output shape (written by the script)

```json
{
  "status": "ok",
  "mode": "deep_research",
  "story_id": "short-slug",
  "category": "<from picks.json>",
  "wp_category_slugs": ["<from picks.json>"],
  "wp_category_ids": [0],
  "topic_theme": "Core topic",
  "primary_keyword": "2-3 word SEO keyword",
  "primary_headline": "Best descriptive headline",
  "primary_asset": "Bitcoin",
  "chart_coin": "bitcoin",
  "sources_used": ["CoinDesk", "CoinTelegraph"],
  "source_urls": ["https://…/primary", "https://…/corroborating"],
  "combined_key_facts": ["Fact from source A", "Fact from source B"],
  "aggregated_raw_content": "Combined clean article text. 600+ words."
}
```

- `category`, `wp_category_slugs`, `wp_category_ids` — copy through from `picks.json` exactly (script does this).
- `source_urls` must list **≥2** resolved publisher URLs when discovery succeeded.
- `combined_key_facts` must have **≥2** entries.

## Manual fallback (last resort only)

If the script fails after 2 aggressive re-runs and self-check still FAILs with zero extracted content, you may manually:

1. Resolve with `resolve_url.py`
2. Extract with `extract_article.py` per URL (parallel)
3. Assemble JSON per the shape above

Do **not** use the internal `web_fetch` tool on publisher URLs (Cloudflare blocks it).

## Step 4 — Final verification thinking block

Before yielding SUCCESS, use a short `<thinking>` block:

- Script ran first (or manual fallback documented).
- `source_urls` has no aggregator wrappers and **≥2** entries.
- `aggregated_raw_content` is clean prose, not HTML/logs.
- `combined_key_facts` has ≥2 items.

Do not write thinking into `OUTPUT_FILE`.

## Step 5 — Yield

Yield `SUCCESS` only on `RESEARCH_CHECK: PASS` (max 3 self-check iterations total). A clean error JSON with a known reason passes the check — yield `SUCCESS` so the orchestrator skips the story cleanly.

**Hard rule:** never write logs, raw HTML, or scrape dumps into `OUTPUT_FILE`.
