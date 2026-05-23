# Tweet Embed via DuckDuckGo (Trusted X Handles)

**Status:** approved — not implemented  
**Created:** 2026-05-23  
**Purpose:** Add a free, CAPTCHA-free trusted tweet embed to crypto news articles using DuckDuckGo search (no Google scraping, no API keys).

When ready to build, tell the agent: *"Implement PLANS/tweet-embed-duckduckgo.md"*

---

## Problem

Articles benefit from a trusted X/Twitter quote embed. Scraping Google directly triggers CAPTCHAs. The free `duckduckgo-search` Python library avoids API keys, registration, and most blocks.

---

## Agreed design decisions

| Decision | Choice |
|----------|--------|
| Where it runs | Orchestrator Python script after writer validation |
| Default | `ENABLE_TWEET_EMBED=1` (opt-out with `ENABLE_TWEET_EMBED=0`) |
| No tweet found | Skip silently; pipeline continues |
| Trusted handles | Fixed crypto **news** list in config file |
| Placement | After 2nd paragraph under H1 hook (before first `## H2`) |
| Max embeds | 0 or 1 per article |
| Retry | One broad retry (drop handle filter, topic-only query) then skip |

---

## Pipeline placement

```
Quill (writer)
  → sync_article_from_raw.py
  → validate_article_structure.py
  → validate_anchor_links.py
  → insert_tweet_embed.py          ← NEW
  → verify_artifacts.py (post_sync)
  → creator (Step 3)
```

Insert **after** sync + structure + anchor validation, **before** existing `post_sync` gate in `workspace-orchestrator/SOUL.md` Step 2.

**Why here:**
- Word count already validated before embed is added
- Writer SOUL still forbids markdown x.com links in body
- Raw HTML embed is injected post-validation, not by Quill

---

## New files (implementation checklist)

| File | Purpose |
|------|---------|
| `workspace-orchestrator/config/trusted_tweet_handles.json` | Editable handle list |
| `workspace-orchestrator/skills/pipeline/get_trusted_tweet.py` | DDGS search + embed HTML builder |
| `workspace-orchestrator/skills/pipeline/insert_tweet_embed.py` | Parse article, insert after hook paragraph 2 |
| `workspace-orchestrator/skills/pipeline/requirements-tweet.txt` | Pin `duckduckgo-search` |

### Default handles (`trusted_tweet_handles.json`)

```json
{
  "handles": [
    "CoinDesk",
    "Cointelegraph",
    "Decrypt",
    "TheBlock__",
    "BeInCrypto",
    "BitcoinMagazine"
  ]
}
```

---

## Search logic (`get_trusted_tweet.py`)

**Inputs:** `primary_keyword` from `validated.json` (fallback: `topic_theme`, then first 3 words of `primary_headline`).

**Query 1 (strict):**

```
site:x.com "{keyword}" (from:CoinDesk OR from:Cointelegraph OR ...)
```

**Query 2 (broad retry, if Query 1 finds nothing):**

```
site:x.com "{keyword}"
```

Only accept URLs where:
- Host is `x.com` or `twitter.com`
- Path contains `/status/`

**Return:** First valid status URL, or `None`.

**Dependency:**

```bash
pip install duckduckgo-search
```

```python
from duckduckgo_search import DDGS

with DDGS() as ddgs:
    results = list(ddgs.text(query, max_results=3))
```

---

## Embed HTML format

Use the official Twitter widget script (not `https://twitter.com` alone):

```html
<blockquote class="twitter-tweet">
  <a href="https://x.com/handle/status/1234567890"></a>
</blockquote>
<script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
```

---

## Insert logic (`insert_tweet_embed.py`)

1. Read `$RUN_DIR/article/final.md` (symlink `/tmp/crypto-article.md`) via `--manifest`
2. Skip META block and H1 line
3. Collect paragraphs until first `## H2`
4. Insert embed block after 2nd non-empty paragraph (skip if fewer than 2 paragraphs)
5. **Idempotent:** if `twitter-tweet` blockquote already present, skip
6. Atomic write back to `final.md`
7. Exit 0 and print:
   - `TWEET_EMBED_OK: <url>` on success
   - `TWEET_EMBED_SKIPPED: <reason>` on skip (still exit 0)

---

## Orchestrator changes

### `init_run.sh` (env block ~line 140)

Add:

```bash
export ENABLE_TWEET_EMBED="${ENABLE_TWEET_EMBED:-1}"
```

### `workspace-orchestrator/SOUL.md` — new Step 2D (after anchor validation)

```bash
if [ "${ENABLE_TWEET_EMBED:-1}" = "1" ]; then
  python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/insert_tweet_embed.py \
    --manifest "$PIPELINE_MANIFEST"
else
  echo "[SKIP] Tweet embed disabled (ENABLE_TWEET_EMBED=0)."
fi
```

---

## Files that stay unchanged

| File | Reason |
|------|--------|
| `workspace-writer/SOUL.md` | Quill still bans markdown x.com links; embed is HTML added by Nexus |
| `workspace-writer/skills/validate_anchor_links.py` | Runs before embed insertion |
| `workspace-wp-publisher/skills/wordpress/publish.sh` | Pandoc + `html_to_gutenberg.py` already wrap raw HTML in `wp:html` blocks |

---

## Registry update (when implemented)

Update `AGENT_PIPELINE_REGISTRY.md`:

- Architecture diagram: tweet step between validate and creator
- Orchestrator pipeline scripts table: add 2 new scripts
- Change log entry
- Known gaps: mark `extract_tweet_quotes.py` as superseded by this feature

---

## Test plan (when implemented)

1. **Unit:** `get_trusted_tweet.py` with mocked DDGS results
2. **Unit:** `insert_tweet_embed.py` paragraph parsing on sample markdown
3. **Integration:** run `insert_tweet_embed.py --manifest` on a real run dir with a known crypto keyword
4. **End-to-end:** full pipeline with `ENABLE_TWEET_EMBED=1`; confirm embed in WordPress post HTML
5. **Fail-open:** keyword with no results → `TWEET_EMBED_SKIPPED`, pipeline completes normally

---

## Change log (this plan)

| Date | Note |
|------|------|
| 2026-05-23 | Plan approved; saved for deferred implementation |
