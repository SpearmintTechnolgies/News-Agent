# Skill: check_article

Combined article validator — writer self-check and orchestrator post-sync gate. **Single home of the self-check loop**; `write-article` and `revise-article` skills reference this file.

## Writer self-check (on raw.md)

```bash
python3 ~/.openclaw/workspace-writer/skills/article/check_article.py \
  --article "$RUN_DIR/article/raw.md" \
  --research "$RUN_DIR/research/validated.json"
```

## Orchestrator gate (on final.md after sync)

```bash
python3 ~/.openclaw/workspace-writer/skills/article/check_article.py \
  --article "$RUN_DIR/article/final.md" \
  --research "$RUN_DIR/research/validated.json" \
  --post-sync
```

## Self-check loop (mandatory before SUCCESS)

1. **Auto-fix mechanical issues first (zero-cost, no rewrite).** Run the deterministic fixer, THEN the validator:
   ```bash
   python3 ~/.openclaw/workspace-writer/skills/article/autofix_article.py --article "$RUN_DIR/article/raw.md"
   python3 ~/.openclaw/workspace-writer/skills/article/check_article.py \
     --article "$RUN_DIR/article/raw.md" \
     --research "$RUN_DIR/research/validated.json"
   ```
   `autofix_article.py` silently repairs em-dashes, the `[Word Count: N]` footer, stray
   `x.com`/`twitter.com` body links, and non-dash bullet formatting — so those never cost you
   a rewrite. Never undo its edits.
2. If the last line is **`ARTICLE_CHECK: PASS`**, yield `SUCCESS`.
3. If **`ARTICLE_CHECK: FAIL`**: fix **ONLY** rules still listed as `FAIL:` — do not rewrite sections that already passed.
4. **Preserve unchanged:** exact H1 text, **Sources:** block, existing valid links, Conclusion, FAQs, and every section not mentioned in FAIL lines.
5. Re-run step 1 (autofix + validator). Repeat at most **3** self-check iterations per spawn.
6. If still failing after 3 iterations, yield the full validator output verbatim (not `SUCCESS`).

## Interpreting output

One line per rule:

```
PASS: h3_count — 4 H3 subsections
FAIL: word_count_footer — set footer to [Word Count: 1102] (footer says 1080)
...
ARTICLE_CHECK: PASS
```

Exit `0` when all rules pass; exit `1` on any failure.

## Rules checked

| Rule | Raw | Post-sync |
|------|-----|-----------|
| H2/H3/FAQ counts and section order | yes | yes |
| Body word band 950–1250 | yes | yes |
| Sources footer | yes | no |
| Word Count footer matches body | yes | no |
| META char limits | yes | no |
| Primary Keyword in SEO Title / slug / H1 | yes | no |
| H1 topic vs research | yes | yes |
| Clean headings (no inline `#`) | yes | yes |
| No bare source-only lines in body | yes | yes |
| Dash bullet lists only (`- item`; no inline `*`) | yes | yes |
| Anchor links (max 2, no tweets, match research) | yes | yes |
| No em-dashes / banned phrases | yes | yes |
