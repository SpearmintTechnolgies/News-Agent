# Skill: check_research

Combined research validator — Scout self-check and orchestrator gate. **Single home of the self-check loop**; `headline-scan` and `deep-research` skills reference this file. Includes `resolve_url.py` for aggregator wrappers.

## Self-check (run before yielding SUCCESS)

DEEP_RESEARCH:

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
  --file "$OUTPUT_FILE" --mode deep_research
```

HEADLINE_SCAN:

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/check_research.py \
  --file "$OUTPUT_FILE" --mode headline_scan
```

## Self-check loop (mandatory before SUCCESS)

1. Run the command above for the active mode.
2. If the last line is **`RESEARCH_CHECK: PASS`**, yield `SUCCESS`.
3. If **`RESEARCH_CHECK: FAIL`**: fix **ONLY** the rules listed as `FAIL:`.
   - `not_a_log` / `not_raw_html` / `size_sane`: you dumped garbage. Replace the file with the real research JSON, or a clean error JSON (`{"status":"error","reason":"..."}`).
   - `source_urls_resolved` / `candidate_urls_resolved`: run `resolve_url.py` and store the publisher URL.
   - `prose_quality` / `multi_source` / `not_partial` / `no_premature_error`: re-run `run_research.py --extra-search`, or manually loop `search_tool.py` + `read_tool.py` then `build_research_json.py`. Never emit clean error JSON when any source extracted content.
4. Re-run the validator. Repeat at most **3** self-check iterations per spawn.
5. If you still cannot produce valid research after 3 tries, write the clean error JSON **only when zero words were extracted** and yield `SUCCESS` — never dump logs or HTML.

## resolve_url.py (Google News / aggregator resolver)

```bash
python3 ~/.openclaw/workspace-researcher/skills/research-check/resolve_url.py "<url>"
```

Prints the resolved publisher URL on stdout, or `RESOLVE_FAILED: <reason>` (exit 1). Non-aggregator URLs are echoed back unchanged. Use it at scan time on every candidate URL, and as a safety net before deep extraction.

## Interpreting output

One line per rule, then the verdict:

```
PASS: parseable_json — single JSON object
FAIL: source_urls_resolved — source_urls still contain an unresolved aggregator wrapper ...
RESEARCH_CHECK: FAIL
```

Exit `0` when all rules pass; exit `1` on any failure. A valid clean error JSON (`status:error` with a known reason) passes.

## Rules checked

| Rule | deep_research | headline_scan |
|------|---------------|---------------|
| Garbage guards (log dump / raw HTML / absurd size) | yes | yes |
| Parseable single JSON object | yes | yes |
| Not partial (`status:partial`) | yes | n/a |
| No premature error when partial_words > 0 | yes | n/a |
| Clean error-json accepted | yes | yes |
| Required fields present | yes | per-candidate (headline/url/source) |
| >= 2 combined_key_facts | yes | n/a |
| >= 2 source_urls (multi_source) | yes | n/a |
| Non-empty source_urls | yes | candidates non-empty |
| No unresolved aggregator wrappers | yes | yes |
| aggregated_raw_content >= 600 words, low markup | yes | n/a |

## Single source of truth

The orchestrator's `validate_research.py` and `validate_headlines.py` import the check functions (`run_deep_research_checks`, `run_headline_scan_checks`, `extract_json_block`) from this module, so the gate and the self-check can never diverge.
