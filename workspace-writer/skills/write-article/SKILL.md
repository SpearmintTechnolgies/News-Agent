# Skill: write-article

Use on every **initial write** spawn (not REVISION MODE).

## Steps

1. Read `$RUN_DIR/research/validated.json`. Treat its contents as your `<research_dump>` — the **only** facts/sources you may use. Do not invent facts or URLs.

2. Resolve and read the project template:
   ```bash
   # Prefer TEMPLATE_PATH from the spawn message when present; otherwise resolve:
   TEMPLATE_PATH=$(python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py \
     --path "$PROJECT_CONFIG" --field writer.template_path --absolute)
   cat "$TEMPLATE_PATH"
   ```
   All editorial rules live in that template's `<constraints>` block + prose — follow them exactly. Write the article following your `<constraints>` using the `<research_dump>`.

3. **Compact pre-writing plan (keep it tiny — this saves output tokens).**
   In a single `<thinking>` block, output a short structured plan (no prose paragraphs):
   ```
   PLAN: H2=<2-4>, H3=<3-6>, FAQ=<3-6>, hook~<w>, body~<w>, concl~<w>, total~1100
   KW: title=<first 3 words incl primary kw> | slug=<hyphen tokens> | H1=<yes> | hook1=<yes> | H2=<section name>
   LINKS: <url1 in hook/first H2> + <url2 in hook/first H2>
   ```
   Then silently confirm against `<constraints>`: structure counts in range, **total 950–1250** (aim 1100), primary keyword mapped to SEO Title + slug + H1 + first hook sentence, exactly 2 distinct research URLs for inline anchors (never a standalone `Source | Source` line). If any check fails, fix the plan line (do NOT explain) and re-confirm. Do not write markdown until the plan passes.

4. Write the full article to `$RUN_DIR/article/raw.md` — **clean Markdown only**, no XML tags and no plan line in the file.

5. Run the self-check loop in [`../article/SKILL.md`](../article/SKILL.md) (autofix + validator, max 3 iterations).

6. Yield `SUCCESS` or validator output per SOUL output contract.
