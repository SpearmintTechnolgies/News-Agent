# Coinography — WordPress REST API Integration

**Site:** https://coinography.com
**Last updated:** June 2026

---

## Section 0 — End-to-End Setup (Start Here)

This is the section a new team member follows from zero. Complete all four parts before running the pipeline.

---

### Part A — Generate Application Password in WordPress Admin

**1. Log in to WordPress admin**

- URL: `https://coinography.com/wp-admin`
- Sign in with the publishing account: `renu@coinography.com`
- This user must have **Author, Editor, or Administrator** role

**2. Open the user profile**

- Left sidebar: **Users → Profile** (for your own account)
- If an admin is setting this up for another user: **Users → All Users → click the user → Edit**

**3. Scroll to the "Application Passwords" section**

On the profile page, below the main account fields, you will find a section titled **Application Passwords** with a text input labelled **"New Application Password Name"**.

If this section is missing, check the following:

| Cause | Fix |
|-------|-----|
| Site is on HTTP, not HTTPS | Application Passwords require HTTPS |
| WordPress version below 5.6 | Update WordPress |
| Security plugin blocking it | Check Wordfence / iThemes settings; allow REST API auth |
| WordPress Multisite | Super-admin may need to enable per site |

**4. Create a new Application Password**

- In the **New Application Password Name** field, enter: `OpenClaw News Pipeline`
- Click **Add New Application Password**

**5. Copy the password immediately**

- WordPress shows the generated password **once only**, formatted with spaces: `xxxx xxxx xxxx xxxx xxxx xxxx`
- You **cannot view it again** after leaving the page — if lost, revoke it and create a new one
- Keep it on your clipboard and proceed to Part B immediately

**6. Confirm your login username**

- The REST API uses the **WordPress login username**, not the display name
- For Coinography this is: `renu@coinography.com`
- Common mistake: using display name "Renu" instead of the login email — this causes 401 errors

---

### Part B — Store the Password in OpenClaw

Run these commands on the machine where the pipeline runs:

```bash
# Create the credentials directory if it doesn't exist
mkdir -p ~/.openclaw/credentials/wp

# Paste the Application Password exactly as WordPress showed it (spaces are fine)
echo -n "xxxx xxxx xxxx xxxx xxxx xxxx" > ~/.openclaw/credentials/wp/coinography.pass

# Restrict permissions to owner read/write only
chmod 600 ~/.openclaw/credentials/wp/coinography.pass
```

| Rule | Detail |
|------|--------|
| File contents | Password only — no username, no JSON, no quotes |
| File permissions | Must be `600` — the loader refuses world-readable password files |
| Git | Never commit this file |
| Spaces | Preserved as-is; `project_config.py` strips only the trailing newline |

---

### Part C — Wire Credentials into Project Config

The password file is referenced in `~/.openclaw/projects/coinography.json` inside the `wordpress` block:

```json
"wordpress": {
  "url": "https://coinography.com",
  "user": "renu@coinography.com",
  "app_password_ref": "credentials/wp/coinography.pass",
  "default_status": "draft",
  "fallback_category_id": 17,
  "picker_category_slugs": ["bitcoin", "ethereum", "xrp", "etf", "..."],
  "categories": [{"id": 17, "name": "Latest News", "slug": "latest-news"}, "..."]
}
```

| Field | Must match |
|-------|------------|
| `url` | Coinography site URL, no trailing slash |
| `user` | Exact WP login username used when creating the Application Password |
| `app_password_ref` | Path relative to `~/.openclaw/` pointing to the `.pass` file |
| `fallback_category_id` | WP category ID used when the article has no resolved category (default 17 = Latest News) |
| `picker_category_slugs` | Curated slugs the Picker may assign as PRIMARY category |
| `categories` | Full live category list (id/name/slug), synced by `sync_wp_categories.py` |
| `default_status` | `draft` — posts are created as drafts until explicitly published |

Post categories are dynamic: the Picker assigns 1 primary + up to 2 secondary categories per article, resolved to numeric IDs and sent in the post payload's `categories` array.

**How this is resolved at runtime:**

- `project_config.py` reads `app_password_ref` from `coinography.json` and opens `~/.openclaw/credentials/wp/coinography.pass`
- `publish.sh` and `wp_post_actions.sh` call `cfg_password()` which invokes `project_config.py`, then pass the result to `curl --user "${WP_USER}:${WP_PASS}"`

---

### Part D — Verify the Connection Works

**Step 1 — Auth test (read-only, safe to run any time)**

```bash
WP_USER="renu@coinography.com"
WP_PASS="$(cat ~/.openclaw/credentials/wp/coinography.pass)"

curl -s --user "${WP_USER}:${WP_PASS}" \
  "https://coinography.com/wp-json/wp/v2/users/me" \
  | python3 -m json.tool
```

| Result | Meaning |
|--------|---------|
| JSON with `"id"`, `"name"`, `"roles"` | Auth works correctly |
| `"code": "rest_not_logged_in"` (401) | Wrong username or wrong password |
| `"code": "rest_forbidden"` (403) | User exists but lacks required role |
| Connection refused / timeout | REST API disabled or site is down |

**Step 2 — Config loader test**

```bash
# Should print: https://coinography.com
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py \
  --slug coinography --field wordpress.url

# Should print the first 4 chars of the password (never log the full password)
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py \
  --slug coinography --password | head -c 4 && echo "…"
```

**Step 3 — Full publish script (optional end-to-end test)**

```bash
bash ~/.openclaw/workspace-wp-publisher/skills/wordpress/publish.sh \
  --project coinography \
  --article /path/to/test-article.md \
  --image /path/to/test-feature.jpg
```

Expected: exit code `0`, draft post URL written to `/tmp/wp-result.txt`.

---

## 1 — Overview

- **What it does:** programmatically creates and updates WordPress posts on `https://coinography.com` — with feature image, Gutenberg block content, and Rank Math SEO fields
- **Integration style:** direct REST API calls via `curl` — no WordPress plugin, no SDK
- **Two API client scripts:**

| Script | Purpose |
|--------|---------|
| `workspace-wp-publisher/skills/wordpress/publish.sh` | Create a new post (draft by default) |
| `workspace-wp-publisher/skills/wordpress/wp_post_actions.sh` | Update an existing post — status, author, or body content |

---

## 2 — Architecture

*(Add architecture diagram image here)*

**Configuration and credentials** live in `projects/coinography.json` and `credentials/wp/coinography.pass`. Both are loaded at runtime by `project_config.py`, which is called by both publishing scripts before any API request is made.

`publish.sh` runs once per article and calls three endpoints in sequence: media upload, post create, Rank Math SEO. `wp_post_actions.sh` runs on demand and calls a single update endpoint.

All results and errors are written to `/tmp/`.

---

## 3 — Configuration Reference

**File:** `~/.openclaw/projects/coinography.json`

```json
"wordpress": {
  "url": "https://coinography.com",
  "user": "renu@coinography.com",
  "app_password_ref": "credentials/wp/coinography.pass",
  "default_status": "draft",
  "fallback_category_id": 17,
  "picker_category_slugs": ["bitcoin", "ethereum", "xrp", "etf", "..."],
  "categories": [{"id": 17, "name": "Latest News", "slug": "latest-news"}, "..."]
}
```

| Field | Coinography value | Purpose |
|-------|-------------------|---------|
| `wordpress.url` | `https://coinography.com` | Site base URL for all API calls |
| `wordpress.user` | `renu@coinography.com` | HTTP Basic Auth username |
| `wordpress.app_password_ref` | `credentials/wp/coinography.pass` | Path to Application Password file |
| `wordpress.fallback_category_id` | `17` | Category used when an article has no resolved category |
| `wordpress.picker_category_slugs` | curated slugs | Categories the Picker may assign as PRIMARY |
| `wordpress.categories` | full live list | Synced from WP by `sync_wp_categories.py` |
| `wordpress.default_status` | `draft` | New posts start as drafts |

**Authors** — used only when promoting a draft to live, not on initial create:

| Label | WordPress user ID |
|-------|-------------------|
| Toby | 3 |
| Ahmed | 17 |
| Golan | 8 |

---

## 4 — Authentication

> For the full setup walkthrough, see Section 0.

- **Method:** WordPress Application Passwords + HTTP Basic Auth
- **curl header:** `--user "renu@coinography.com:<app_password>"`
- **Password storage:** `~/.openclaw/credentials/wp/coinography.pass` (mode `600`, never committed to git)
- **Loader:** `project_config.py` reads `app_password_ref` from the project config and opens the password file at runtime
- **Username format:** WP login email — not display name
- **WP requirements:** HTTPS, WordPress 5.6+, Author/Editor role, Application Passwords section visible in user profile

---

## 5 — API Base URLs

```
https://coinography.com/wp-json/wp/v2        WordPress Core REST API
https://coinography.com/wp-json/rankmath/v1  Rank Math SEO Plugin API
```

---

## 6 — Endpoints Reference

| Endpoint | Method | Script | Purpose |
|----------|--------|--------|---------|
| `/wp/v2/media` | POST | `publish.sh` | Upload feature image (binary JPEG) |
| `/wp/v2/media/{id}` | POST | `publish.sh` | Set `alt_text` on the uploaded image |
| `/wp/v2/posts` | POST | `publish.sh` | Create a new post |
| `/wp/v2/posts/{id}` | POST | `wp_post_actions.sh` | Update post status, author, or body |
| `/rankmath/v1/updateMeta` | POST | `publish.sh` | Write SEO title, description, focus keywords |

Categories and authors are passed as **numeric IDs inside JSON payloads** — there are no separate calls to `/categories` or `/users`.

---

## 7 — Create Flow — `publish.sh`

### Phase 0 — Content preparation (local, no API calls)

- Parse the writer META block: SEO Title, Meta Description, URL Slug, Primary/Secondary Keywords
- Strip: META block, Sources footer, Word Count line, chart placeholders
- Post title = article H1 (first `#` heading in the markdown)

### Phase 1 — Upload feature image

```
POST /wp/v2/media
Content-Disposition: attachment; filename=coinography-feature.jpg
Content-Type: image/jpeg
Body: binary JPEG (minimum 51 KB)
```

- **Success:** HTTP 201 → extract `id` → used as `featured_media` in the post payload
- **Auth failure (401 / 403) or any other error:** log and continue **without** a featured image

**Set alt text immediately after upload:**

```json
POST /wp/v2/media/{id}
{"alt_text": "<primary keyword or post title>"}
```

### Phase 2 — Content conversion (local, no API calls)

- **Pandoc:** markdown → HTML
- **Strip duplicate H1** if it matches the post title — Divi theme renders the title above the featured image, so keeping the H1 in the body would show the headline twice
- **`html_to_gutenberg.py`:** HTML → Gutenberg block markup — required for correct full-width layout on Coinography's Divi theme

### Phase 3 — Create post

```json
POST /wp/v2/posts
{
  "title":          "<article H1>",
  "content":        "<gutenberg HTML>",
  "excerpt":        "<meta description>",
  "status":         "draft",
  "categories":     [190, 189],
  "slug":           "<optional, from META block>",
  "featured_media": <media_id from Phase 1, omitted if no image>
}
```

- **`categories`** = the article's resolved `wp_category_ids` read from `validated.json` (Picker's 1 primary + up to 2 secondary). Falls back to `[fallback_category_id]` (17) when absent.
- **Success:** HTTP 201 → returns `id` and `link`
- **Retries:** up to 3× with exponential backoff (5s → 10s → 20s) on transient errors
- **Fatal (no retry):** HTTP 401, 403, 404, 422

### Phase 4 — Rank Math SEO

```json
POST /rankmath/v1/updateMeta
{
  "objectID":   <post_id>,
  "objectType": "post",
  "meta": {
    "rank_math_title":               "<SEO Title from META>",
    "rank_math_description":         "<Meta Description from META>",
    "rank_math_focus_keyword":       "<primary, secondary keywords>",
    "rank_math_facebook_title":      "<same as SEO Title>",
    "rank_math_twitter_title":       "<same as SEO Title>",
    "permalink":                     "<url slug>"
  }
}
```

- Requires the **Rank Math SEO** plugin to be active on Coinography
- Fatal if the META block is missing SEO Title, Meta Description, or keywords

### Phase 5 — Output files

- `/tmp/wp-result.txt` — post URL string
- `/tmp/wp-result.json` — structured result (also saved to run dir as `publish/wordpress.json`)

Key fields in `wp-result.json`:

| Field | Meaning |
|-------|---------|
| `post_id` | WordPress post ID |
| `draft_url` | URL to the draft post |
| `post_status` | `draft` or `publish` |
| `feature_image_uploaded` | `true` if image upload succeeded |
| `rank_math_applied` | `true` if SEO fields were written |
| `content_format` | `gutenberg_blocks` when block conversion ran |
| `seo_title` | SEO Title from META (sent to Rank Math only, not the visible post title) |
| `focus_keyword` | Primary keyword |
| `article_headline` | H1 used as the WordPress post title |

---

## 8 — Update Flow — `wp_post_actions.sh`

Three operational modes:

| Operation | API call | Payload |
|-----------|----------|---------|
| Unpublish / revert to draft | `POST /wp/v2/posts/{id}` | `{"status": "draft"}` |
| Publish live | `POST /wp/v2/posts/{id}` | `{"status": "publish", "author": 3\|17\|8}` |
| Update body after edit | `POST /wp/v2/posts/{id}` | `{"content": "<new gutenberg HTML>"}` |

Content updates run the same Phase 2 conversion (markdown → HTML → Gutenberg) before sending the payload.

**CLI examples:**

```bash
# Revert to draft
bash ~/.openclaw/workspace-wp-publisher/skills/wordpress/wp_post_actions.sh \
  --post-id 12345 --set-status draft --project coinography

# Publish live as Ahmed (author ID 17)
bash ~/.openclaw/workspace-wp-publisher/skills/wordpress/wp_post_actions.sh \
  --post-id 12345 --set-status publish --author 17 --project coinography

# Replace body content from an edited markdown file
bash ~/.openclaw/workspace-wp-publisher/skills/wordpress/wp_post_actions.sh \
  --post-id 12345 --markdown /tmp/edited-article.md --update-content --project coinography
```

---

## 9 — Draft vs Publish Lifecycle

| Stage | WP status | Author | Script |
|-------|-----------|--------|--------|
| Pipeline creates post | `draft` | API user (`renu@coinography.com`) | `publish.sh` |
| Editorial promotes to live | `publish` | Toby (3), Ahmed (17), or Golan (8) | `wp_post_actions.sh --set-status publish --author N` |
| Editorial reverts | `draft` | unchanged | `wp_post_actions.sh --set-status draft` |

---

## 10 — Error Handling Summary

| Condition | Behavior |
|-----------|----------|
| HTTP 401 / 403 on post create | Fatal — exit 1, no retry |
| HTTP 404 (REST API not found) | Fatal — exit 1 |
| HTTP 422 (invalid payload) | Fatal — exit 1 |
| Transient error on post create | Retry up to 3× with backoff |
| Transient error on Rank Math | Retry up to 3× with backoff |
| Image upload failure | Log warning, continue without featured image |
| Missing META SEO fields | Fatal before Rank Math call |
| Project slug ≠ run manifest | Fatal before any API call |

Errors are written to `/tmp/wp-error.log`. The post URL on success is in `/tmp/wp-result.txt`.

---

## 11 — WordPress Site Prerequisites

- **HTTPS** enabled on `coinography.com` — required for Application Passwords
- Publishing user (`renu@coinography.com`) with **Author/Editor/Admin** role
- **Application Password** created and stored — see Section 0
- REST API enabled (default on modern WordPress — verify with `GET https://coinography.com/wp-json/wp/v2`)
- **Rank Math SEO** plugin active — required for `/rankmath/v1/updateMeta`
- **Divi theme** installed — Gutenberg block content is required for correct full-width post layout
- Category ID **17** exists as the default post category — verify in WP admin → Posts → Categories

---

## 12 — Manual Smoke Tests

**Test 1 — Verify auth**

```bash
WP_USER="renu@coinography.com"
WP_PASS="$(cat ~/.openclaw/credentials/wp/coinography.pass)"

curl -s --user "${WP_USER}:${WP_PASS}" \
  "https://coinography.com/wp-json/wp/v2/users/me" | python3 -m json.tool
```

Expected: JSON with `"id"`, `"name"`, `"roles"`.

**Test 2 — Upload a test image**

```bash
WP_USER="renu@coinography.com"
WP_PASS="$(cat ~/.openclaw/credentials/wp/coinography.pass)"

curl -s \
  --user "${WP_USER}:${WP_PASS}" \
  --request POST "https://coinography.com/wp-json/wp/v2/media" \
  --header "Content-Disposition: attachment; filename=test.jpg" \
  --header "Content-Type: image/jpeg" \
  --data-binary @/path/to/test.jpg \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print('Media ID:', r.get('id'))"
```

**Test 3 — Create a test draft post**

```bash
WP_USER="renu@coinography.com"
WP_PASS="$(cat ~/.openclaw/credentials/wp/coinography.pass)"

curl -s \
  --user "${WP_USER}:${WP_PASS}" \
  --request POST "https://coinography.com/wp-json/wp/v2/posts" \
  --header "Content-Type: application/json" \
  --data '{"title":"Test Post — Delete Me","content":"<p>API smoke test.</p>","status":"draft","categories":[17]}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print('Post ID:', r.get('id'), '| URL:', r.get('link',''))"
```

**Test 4 — Full script end-to-end**

```bash
bash ~/.openclaw/workspace-wp-publisher/skills/wordpress/publish.sh \
  --project coinography \
  --article /path/to/test-article.md \
  --image /path/to/test-feature.jpg

cat /tmp/wp-result.txt   # post URL on success
cat /tmp/wp-error.log    # error reason on failure
```

---

## 13 — File Reference

| File | Role |
|------|------|
| `~/.openclaw/projects/coinography.json` | Site URL, user, category ID, password file reference |
| `~/.openclaw/credentials/wp/coinography.pass` | Application Password (not in git, mode 600) |
| `~/.openclaw/workspace-orchestrator/skills/pipeline/project_config.py` | Runtime config and password loader |
| `~/.openclaw/workspace-wp-publisher/skills/wordpress/publish.sh` | Create post API client |
| `~/.openclaw/workspace-wp-publisher/skills/wordpress/wp_post_actions.sh` | Update post API client |
| `~/.openclaw/workspace-wp-publisher/skills/wordpress/html_to_gutenberg.py` | Markdown HTML → Gutenberg block converter |

---

*The same project-driven pattern (`projects/<slug>.json` + `credentials/wp/<slug>.pass`) applies to any additional WordPress site — no code changes required.*
