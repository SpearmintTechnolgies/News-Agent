# Projects

Each `projects/<slug>.json` defines a publishing target (a WordPress site). The pipeline is project-scoped — adding a new site is a configuration change, not a code change.

## Run syntax

```
run pipeline N                  # defaults to coinnetwork (active test site)
run pipeline <slug> N           # run pipeline N times for project <slug>
                                # e.g. run pipeline coinography 1 for the main site
```

The orchestrator parses `<slug>` in Step 0.4, validates `projects/<slug>.json`, and locks it into `manifest.json` for the whole run.

**Active defaults (2026 testing):** `coinnetwork` → https://coinnetwork.info is the default when no slug is given. `coinography` → https://coinography.com remains configured but with `"enabled": false` so pool scan / `--all` feed skip it until you re-enable for production.

## Add a new site (recommended: automated onboarding)

The deterministic onboarding engine collects everything below, verifies WordPress live, curates categories/authors, writes all config + credentials, and patches `openclaw.json` — with zero LLM involvement. Two ways to run it:

**Telegram (no terminal needed):** send `/onboard` from an existing bound project group (e.g. the Coinography group). No `@bot` mention is required — the command is registered as a plugin command and bypasses the mention gate. The bot replies with a prerequisites brief, then walks through the rest step by step. For free-text answers, **reply to the bot's message** (required in `requireMention: true` groups). At the **logo/watermark** step, reply with a transparent PNG sent as **File** (paperclip → File) or **Photo**. Owner-only; handled by the `project-onboarder` plugin without waking the orchestrator.

**Terminal wizard:** the same flow includes a logo step — provide a local path to a `.png` file when prompted. The wizard saves it to `assets/logo-{slug}.png` and sets `creator.logo_path` in the project JSON.

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/onboard_project.py wizard
```

Either path writes `projects/<slug>.json`, `credentials/wp/<slug>.pass`, `state/recent_topics-<slug>.json`, then prints (or applies, on confirmation) the exact `openclaw.json` patch and restarts the gateway. See [`ONBOARDING_CHECKLIST.md`](ONBOARDING_CHECKLIST.md) for the full field-by-field reference and the manual fallback procedure.

Verify any project (new or existing) at any time:
```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/validate_project_config.py --slug <slug> --openclaw-sync --scanner-ready --live
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/onboard_project.py verify --slug <slug>
```

## Add a new site manually (fallback — 4 steps)

### 1. Copy the template

```bash
cp ~/.openclaw/projects/coinography.json ~/.openclaw/projects/<newslug>.json
```

Edit at least:

| Field | What to change |
|-------|----------------|
| `slug`, `name` | unique slug + human label |
| `wordpress.url`, `wordpress.user` | new site URL + WP user |
| `wordpress.app_password_ref` | `credentials/wp/<newslug>.pass` |
| `wordpress.fallback_category_id` | category ID used when no pick category is available (usually the site's "Latest News") |
| `wordpress.categories` | DO NOT hand-edit — populated by `sync_wp_categories.py` (step 1b) |
| `wordpress.picker_category_slugs` | curated subset of category slugs the Picker may assign as PRIMARY category |
| `research.rss_feeds` | RSS feeds for this niche |
| `research.exclude_keywords` | terms to filter out (e.g. memecoin sites exclude `bitcoin etf`) |
| `picker.diversity_window_hours` | hours a primary category is excluded from re-use (default 72) |
| `writer.template_path` | path to the writer template (optional — falls back to Coinography template) |
| `telegram.card_prefix` | label that opens every Telegram card, e.g. `[MemeCoinist]` |
| `authors` | per-site WP author profiles for the publish picker |

### 2. Store the WP application password

```bash
echo -n "Wxxx YYYY ZZZZ AAAA BBBB CCCC" > ~/.openclaw/credentials/wp/<newslug>.pass
chmod 600 ~/.openclaw/credentials/wp/<newslug>.pass
```

The file is read by the project-config loader (`wordpress.app_password_ref`). Never commit this file to git.

### 2b. Sync the site's WordPress categories

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/sync_wp_categories.py --slug <newslug>
```

This fetches the live category list (id/name/slug) via the WP REST API and writes it to `wordpress.categories`. Then hand-pick the slugs the Picker should use into `wordpress.picker_category_slugs`, and set `wordpress.fallback_category_id`. Re-run this any time the site's categories change (it only rewrites `categories`, never your curated `picker_category_slugs`).

### 3. (Optional) Add a writer template

If the site needs a distinct voice, copy `workspace-writer/templates/COINOGRAPHY_TEMPLATE.md` to a new path and point `writer.template_path` at it.

### 4. Smoke-test

```bash
run pipeline <newslug> 1
```

The Telegram card should open with the project's `card_prefix`. The publish-author picker should show the new site's `authors[]`.

## Schema reference

See `AGENT_PIPELINE_REGISTRY.md` → **Projects** section for the field-by-field schema and which agent reads each section.

## Isolation guarantees (what the system handles for you)

- Run directories are scoped: `/tmp/<slug>-run-<RUN_ID>/`.
- URL dedup is per-project: `(url, project)` composite key in `article_history.db`.
- Recent-category diversity is per-project: same Picker category can run on Site A and Site B back-to-back without false dedup.
- Credentials are read at runtime from the project config — `publish.sh` and `wp_post_actions.sh` have zero hardcoded URLs/users/passwords.

## Common mistakes

- **Forgetting `chmod 600` on the `.pass` file.** The loader will refuse to read world-readable password files.
- **Reusing a `slug`.** The loader caches on slug; collisions silently use the wrong config.
- **Empty `picker.allowed_categories`.** With no allowlist, every category passes through. Set the list explicitly.
- **Editing `manifest.json` mid-run.** The project is locked at Step 0; manifest edits are ignored downstream.

## Retiring a site

1. Stop running `run pipeline <slug> N` for it.
2. Archive the config + credentials (move out of `projects/` and `credentials/wp/`).
3. Existing DB rows tagged with that `project` slug stay — they're history, not active runs.

## Parking a site (e.g. main while testing)

Set `"enabled": false` in `projects/<slug>.json`. Keep the file and credentials. Pool scan and `--all` feed skip it; explicit `run pipeline <slug> N` still works. To go live on Coinography again: set `"enabled": true`, set `DEFAULT_PROJECT_SLUG = "coinography"` in `project_config.py`, and prefer the Coinography Telegram group.
