# Skill: wordpress

## What This Skill Does
Publishes a crypto news article to WordPress as a **live** post (`status: publish` by default). Uploads the **feature image** only (no inline price charts), strips the writer META block from the body, converts markdown to **Gutenberg blocks** (same format as pasting into the default block editor), creates the post, then sets **Rank Math** SEO via `POST /wp-json/rankmath/v1/updateMeta`.

## How to Invoke

```bash
bash ~/.openclaw/workspace-wp-publisher/skills/wordpress/publish.sh
```

All arguments are **optional** — excerpt, slug, keywords, and titles are read from `/tmp/crypto-article.md`. **Post title** = first `#` H1 in the article body. **SEO title** (META) goes to Rank Math only, not the WordPress post title or body headline.

## Pipeline Phases

1. **Parse META** — SEO Title, Meta Description, URL Slug, Primary Keyword, Secondary Keywords; extract article H1 from first `#` heading; strip META, **Sources** footer, and Word Count from body (in-body source links remain).
2. **Images** — Feature image upload; any `[CHART_PLACEHOLDER]` or `/tmp/chart.png` references stripped from body.
3. **HTML** — Pandoc → HTML; strip body H1 when it matches post title (Divi shows title above featured image); **`html_to_gutenberg.py`** → Gutenberg blocks.
4. **WordPress post** — `POST /wp/v2/posts` with **title = article H1** (visible headline above image; not SEO Title).
5. **Rank Math** — `rank_math_title` = META SEO Title; focus keywords comma-separated in `rank_math_focus_keyword` (not WP Tags). Social mirrors use SEO title/description.

## Layout note (Divi theme)

- **Single headline:** Post title = article H1 (theme renders it above the featured image). The same H1 is removed from post body so you do not get a second heading after the image.
- **“, Divi” in the posts list** only means you opened **Divi Builder** on that draft — not a different post type.
- **Narrow one-sided layout** on old API posts was caused by **raw HTML** in `content`. New publishes use **Gutenberg blocks** so layout matches manual paste in the default editor.
- Optional wp-admin: **Divi Post Settings → Layout → Fullwidth** for extra full-page width.

## `/tmp/wp-result.json`

| Field | Meaning |
|---|---|
| `content_format` | `gutenberg_blocks` when block conversion ran |
| `rank_math_applied` | Rank Math SEO updated |
| `article_headline` | H1 used as WordPress post title |
| `seo_title` | META SEO Title (Rank Math only) |
| `focus_keyword` | Primary keyword from META |
| `secondary_keywords` | Raw Secondary Keywords line from META |
| `rank_math_focus_keyword` | Full comma-separated string sent to Rank Math Focus Keyword |
| `meta_stripped` | META removed from body |
| `sources_footer_stripped` | Sources list footer removed (links stay in article text) |

## Return Values

| Exit Code | Meaning |
|---|---|
| `0` | Draft URL in `/tmp/wp-result.txt` |
| `1` | Error in `/tmp/wp-error.log` |

## Post Configuration

| Setting | Value |
|---|---|
| Site | `https://slateblue-reindeer-775070.hostingersite.com` |
| Status | `publish` (use `--status draft` for review-first) |
| Category | ID `3` (Crypto News) |
