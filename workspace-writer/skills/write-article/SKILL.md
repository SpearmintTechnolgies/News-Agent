# Skill: write-article

Use on every **initial write** spawn (not REVISION MODE).

## Steps

1. Read `$RUN_DIR/research/validated.json`. Treat its contents as your `<research_dump>` — the **only** facts/sources you may use. Do not invent facts or URLs.

2. Read the resolved project template (`writer.template_path`). Follow all editorial rules specified in the template `<constraints>` block and prose.

3. **Pre-Flight Plan & Checklist (in `<thinking>`)**
   Before writing Markdown, complete the Pre-Flight Checklist defined in `SOUL.md` in a short `<thinking>` block:
   ```text
   PRE-FLIGHT PLAN:
   - [ ] SEO Title: "<Title>" (<count> chars <= 55, keyword in first 3 words, includes number)
   - [ ] URL Slug: "<slug>" (<count> chars <= 50, keyword tokens included)
   - [ ] Meta Desc: "<Desc>" (<count> chars <= 155, keyword verbatim included)
   - [ ] Hook 1st sentence: "<Sentence starting with primary keyword>"
   - [ ] Sections: H2=<2-4>, H3=<3-6>, FAQs=<3-6>, Target words=1100 (950-1250)
   - [ ] Sources: 2 URLs from source_urls mapped into hook or first H2
   ```

4. Write the full article in **clean Markdown** directly to `$RUN_DIR/article/raw.md`. No XML tags or thinking blocks inside the markdown file.

5. Run the self-check loop in [`../article/SKILL.md`](../article/SKILL.md) (autofix + check_article.py).

6. Yield `SUCCESS` or validator output per SOUL output contract.
