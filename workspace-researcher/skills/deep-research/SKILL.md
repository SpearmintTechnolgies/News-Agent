# Skill: deep-research

Use when the spawn message starts with `MODE: DEEP_RESEARCH` (also the default if no `MODE:` line is present). Do a full deep dive on ONE story the Picker already chose. You do **not** re-pick.

**Primary path:** run `run_research.py --self-check` first — one exec does search, read, build, and validate. Do **not** use any other tool or skill before Step 1 completes.

## Spawn message

```
MODE: DEEP_RESEARCH
INPUT_FILE: <absolute path to picks.json>
PICK_INDEX: <integer, 1-based>
OUTPUT_FILE: <absolute path to write raw.json for this iteration>
```

## Target (stop the moment both are true)

- **>= 600 words** of clean article prose, AND
- **>= 2 distinct-domain sources**.

Hard caps (enforced by `run_research.py`): **max 6 Jina attempts**, **max 10 URL tries**, **~90s wall-clock budget**.

## Step 0 — Thinking block (REQUIRED)

Before any exec, a **short** `<thinking>` block: confirm mode + spawn paths + headline only. No journey narrative. Never write thinking into `OUTPUT_FILE`.

## Step 1 — Run primary research (REQUIRED FIRST AND ONLY EXEC on happy path)

**This must be your first exec command.** Do not read RSS, browse manually, call `web-reader-pro`, `extract_article.py`, or `run_deep_research.py` before this completes.

```bash
DEEP=~/.openclaw/workspace-researcher/skills/deep-research
python3 "$DEEP/run_research.py" \
  --input "$INPUT_FILE" --pick-index $PICK_INDEX --output "$OUTPUT_FILE" \
  --self-check
```

Watch stderr:
- `RESEARCH_OK` + `RESEARCH_CHECK: PASS` → yield `SUCCESS` immediately (Step 2). **Do not run `check_research.py` again.**
- `RESEARCH_PARTIAL` or `RESEARCH_CHECK: FAIL` → Step 2.

## Step 2 — Retry or yield

**If `RESEARCH_PARTIAL`** (at most once):

```bash
python3 "$DEEP/run_research.py" \
  --input "$INPUT_FILE" --pick-index $PICK_INDEX --output "$OUTPUT_FILE" \
  --extra-search --self-check
```

- If stderr ends with `RESEARCH_CHECK: PASS` → yield `SUCCESS`.
- If still FAIL → fallback ladder (below), then yield only when check passes.

## Fallback ladder (ONLY after Step 1 + Step 2 still FAIL)

Use these **in order**, only when primary path did not pass the check:

1. **Manual search/read loop** — max 6 total reads, sequential:
   ```bash
   SRC_DIR="$(dirname "$OUTPUT_FILE")/sources"
   mkdir -p "$SRC_DIR"
   python3 "$DEEP/search_tool.py" --query "<headline keywords>" --max 8
   python3 "$DEEP/read_tool.py" --url "<url>" --out-dir "$SRC_DIR" --source "<domain>"
   python3 "$DEEP/build_research_json.py" \
     --input "$INPUT_FILE" --pick-index $PICK_INDEX \
     --out-dir "$SRC_DIR" --output "$OUTPUT_FILE"
   python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
     --file "$OUTPUT_FILE" --mode deep_research
   ```

2. **Pick corroborating_sources** — read listed URLs with `read_tool.py`, rebuild, self-check.

3. **Last resort per URL only** — `skills/fallback/web-reader-pro/` or `extract_article.py`.

Never use fallbacks as the first move. Never fabricate a second source.

## Forbidden before Step 1 completes

- `extract_article.py`, `run_deep_research.py` (deprecated)
- `web-reader-pro` skill
- RSS fetches, `web_fetch`, manual browsing, separate `check_research.py` before Step 1 finishes

## Failure handling

- **A link fails** → `read_tool` already tried Jina then trafilatura; read the next result.
- **Still short after caps** → clean error JSON when zero content → yield `SUCCESS`.

**Hard rules:**
- Never fabricate data, quotes, URLs, or a second source.
- Never write logs, raw HTML, or scrape dumps into `OUTPUT_FILE`.

## Output shape (written by build_research_json.py)

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
  "sourced_facts": [
    {
      "id": "fact_001",
      "text": "Fact from source A",
      "source_url": "https://…/primary",
      "source_domain": "example.com",
      "source_record": "research/sources/<sha1>.json"
    }
  ],
  "aggregated_raw_content": "Combined clean article text. 600+ words."
}
```

`combined_key_facts` remains a plain `list[str]` for all existing consumers. Optional `sourced_facts` adds provenance for future verification and must not replace it.

## Claim verification (orchestrator)

After `validate_research` writes `research/validated.json`, FEED_DRAIN runs:

```bash
python3 ~/.openclaw/workspace-researcher/skills/deep-research/qualify_claims.py \
  --validated "$RUN_DIR/research/validated.json" \
  --output "$RUN_DIR/research/qualified_claims.json"

python3 ~/.openclaw/workspace-researcher/skills/deep-research/verify_claims.py \
  --validated "$RUN_DIR/research/validated.json" \
  --sources-dir "$RUN_DIR/research/sources" \
  --qualified "$RUN_DIR/research/qualified_claims.json" \
  --output "$RUN_DIR/research/verification.json"
```

`qualify_claims` is additive (`sourced_facts` unchanged). Only `decision=VERIFY` claims are verified. Quill still reads `validated.json` unchanged.