# Skill: revise-article

Use when the spawn message contains **`REVISION MODE`**.

## Steps

1. Read `$RUN_DIR/research/validated.json` — facts must stay accurate; do not invent sources.

2. Read the **baseline article** at `$RUN_DIR/article/raw.md` (or `final.md` if the spawn says so). Do **not** full-rewrite from scratch.

3. Read the **feedback** verbatim from the spawn message (usually one `FAIL:` line from `check_article.py`). Apply **only** that fix.

## PRESERVE (unless feedback explicitly names the element)

- Exact **H1** title text (must still match research headline)
- Entire **Sources:** block and URLs listed there
- **`[Word Count: N]`** footer (recompute only if you change body length)
- Existing in-body source markdown links (unless anchor rule failed)
- All sections **not** mentioned in the FAIL line (Conclusion, FAQs, unrelated H2/H3)

4. Follow the active project template. Preserve body length within **±50 words** of the baseline unless feedback requires a length change.

5. Overwrite `$RUN_DIR/article/raw.md`.

6. Run the self-check loop in [`../article/SKILL.md`](../article/SKILL.md) (max 3 iterations).

7. Yield `SUCCESS` or validator output per SOUL output contract.
