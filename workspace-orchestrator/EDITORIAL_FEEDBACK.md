# EDITORIAL_FEEDBACK.md — News Card Editorial Feedback

Handles Telegram feedback on news cards sent after pipeline Step 6. **Part of the Step 6 success path** (card send) but **not** a pipeline step itself — do not spawn subagents for editorial actions.

---

## When this applies

| Pattern | Example |
|---------|---------|
| Callback `oc_r:` / `oc_ri:` / `oc_ri_menu:` | Rate article or image |
| Callback `oc_draft:` / `oc_draft_yes:` / `oc_draft_no:` | Unpublish flow |
| Callback `oc_publish:` / `oc_pub_a:` / `oc_pub_y:` / `oc_pub_n:` / `oc_noop:` | Publish flow (in-card author picker) |
| Callback `oc_edit:` / `oc_edit_apply:` / `oc_edit_cancel:` | Edit flow |
| Callback `oc_go:` | Feed card: run this single headline — handler enqueues a job; **do NOT** start the pipeline here |
| Callback `oc_feed_refresh:` | Feed card: refresh headlines in place (legacy batch cards) |
| Text | `RATE 8`, `IMAGE 7`, `DRAFT`, `PUBLISH`, `EDIT` |
| Document reply | `.md` file replying to bot's edit prompt |

**Channel:** All of this happens in the **news-agent GROUP** — never DM. The per-story pipeline approval gate (Step 2.5) is now in the group too.

---

## What to do

1. **Do NOT** start or continue the crypto news pipeline.
2. **Do NOT** spawn subagents.
3. Run the handler immediately:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/handle_card_feedback.py \
  --payload "<callback data if present>" \
  --message-text "<user text if present>" \
  --chat-id "<telegram chat id>" \
  --user-id "<telegram user id>" \
  --username "<telegram username if known>" \
  --reply-to-message-id "<replied-to message id if present>" \
  --document-file-id "<telegram file_id if document upload>" \
  --document-name "<original filename if document upload>"
```

4. **If the handler already sent a Telegram reply, do NOT repeat stdout in the group — end turn silently.**
5. **If stdout is exactly `NO_REPLY`, end turn silently** — do NOT send `NO_REPLY` via the `message` tool or as plain text (that causes "Message failed" errors).
6. If handler failed before replying (and stdout is not `NO_REPLY`), show the stdout line to the user.

### Feed-card single-click (`oc_go:` / `oc_feed_refresh:`)

These come from the hourly headline feed cards (see `send_feed_card.py`).

**Normal path (zero tokens):** The **`feed-tap-claimer`** OpenClaw plugin (`~/.openclaw/plugins/feed-tap-claimer/`) intercepts **all news-card and feed-card callback taps** (`oc_publish`, `oc_pub_a`, `oc_r`, `oc_draft`, `oc_edit`, `oc_go`, etc.) **before** they reach the orchestrator. It runs `handle_card_feedback.py` in-process via the `before_dispatch` hook and returns `{ handled: true }`, so **no LLM turn is started**. Restart the gateway after install/enable changes.

**Fail-open fallback (this doc):** If the plugin is disabled, errors, or the tap is not a recognized card prefix, routing falls through to the orchestrator exactly as before. Run the handler immediately:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/handle_card_feedback.py \
  --payload "<callback data>" \
  --chat-id "<telegram chat id>" \
  --user-id "<telegram user id>" \
  --username "<telegram username if known>" \
  --reply-to-message-id "<message id if known>"
```

- `oc_feed_refresh:` (legacy batch cards) is **fully handled** by `handle_card_feedback.py`. It prints `FEED_CARD_REFRESHED`. End your turn — do NOT start the pipeline.
- `oc_go:{feed_id}:{index}` (Run this story) is **fully handled** by `handle_card_feedback.py`. It claims the card index, enqueues a `feed_jobs` row, edits the button to **Queued**, posts "Queued on …" to the group, and best-effort calls `dispatch_feed_jobs.py` (starts a drainer only if none is live). It prints `FEED_JOB_ENQUEUED: …` or `FEED_GO_DUPLICATE: …`. **End your turn — do NOT start the pipeline.** A separate isolated worker runs `FEED_DRAIN` (see SOUL.md → Feed drain entry).

---

## Pipeline vs feedback

| User says / incoming | Route |
|-----------|-------|
| `run pipeline`, `run crypto news pipeline` | SOUL.md pipeline (pool-backed) |
| `oc_go:` / `oc_feed_refresh:` feed-card tap | **`feed-tap-claimer` plugin** (normal); orchestrator + this doc (fail-open fallback) — pipeline starts on `FEED_DRAIN` cron |
| Card callbacks (`oc_publish`, `oc_r`, `oc_draft`, `oc_edit`, …) | **`feed-tap-claimer` plugin** (normal); orchestrator + this doc (fail-open fallback) |
| `AUTO_RUN ...` (idle watchdog cron) | SOUL.md Auto-run entry |
| "fetch/latest/refresh news" | SOUL.md Refresh-feed entry (`send_feed_card.py`) |
| RATE, IMAGE, DRAFT, PUBLISH, EDIT, `oc_go`/`oc_feed_refresh` taps, edit `.md` upload | This doc |

Pipeline takes precedence only when the message explicitly requests the pipeline.

**Channels:** Pipeline gate (Step 5 yes/no) is in the **Telegram DM** with Nexus. News cards and editorial feedback run in the **`news-agent` group** (`config/telegram_card_config.json`). RATE/PUBLISH/EDIT in DM will not resolve articles unless you reply to the card in the group.

---

## PUBLISH flow notes

- Tap **Publish** or reply `PUBLISH` to the card.
- The card keyboard swaps to **Toby** / **Ahmed** / **Golan** author buttons in place (plus **Back**).
- Tap author → publishes **immediately** (no second confirm). Card action row becomes **Published**.
- Before status flip, handler runs `--ensure-featured-image` so the live post keeps the feature image.
- WordPress receives `status: publish` + `author: 3|17` on the project site via `wp_post_actions.sh`.
- If already published → bot replies "already published".
- Draft posts are created by the pipeline as author renu (API user); byline changes only at Telegram publish.

### Callback prefixes

| callback_data | Action |
|---------------|--------|
| `oc_publish:{run_id}` | Show author picker on card |
| `oc_pub_a:{run_id}:{author_id}` | Publish live with author (direct) |
| `oc_pub_y:{run_id}:{author_id}` | Publish live (legacy alias) |
| `oc_pub_n:{run_id}` | Back — restore card action row |
| `oc_noop:{run_id}` | No-op (inert Published button) |

---

## EDIT flow notes

- Tap **Edit** or reply `EDIT` to the news card.
- Bot sends `article-{run_id}.md` as a document.
- Download → edit → **save** → reply to that document with a corrected **`.md` file** (not pasted text).
- Identical upload (no changes) → bot replies "No changes detected" and keeps the edit session open (`EDIT_UNCHANGED`).
- Changed upload → bot shows **+N / −M lines** summary plus **Apply to WordPress** / **Cancel** buttons.
- After apply on a draft → bot offers a **Publish** button (author picker follows).
- Pasted text during edit → handler returns `EDIT_USE_DOCUMENT`.
- Second edit compares against the latest **applied** content (not stale snapshot).

### Document reply — CRITICAL routing rule

When the user replies to the bot's edit prompt with a `.md` document:

1. **Always** call the handler with `--document-file-id` from the **user's uploaded document** in the current message metadata.
2. **Never** read `media/inbound/tmp*.md` from reply context — that is often the **bot's original unedited file** bundled with the user's upload.
3. **Never** call `handle_edit_document()` manually with a local path. The handler downloads the file from Telegram via `file_id`.
4. Set `--reply-to-message-id` to the **bot edit prompt message id** (the document the user replied to).

Wrong file selection causes false `EDIT_UNCHANGED` even when the user's saved file has real edits.

---

## DRAFT flow notes

- Tap **Unpublish** or reply `DRAFT` to the card.
- Confirm with **Yes, unpublish** / **Cancel**.
- Sets WordPress post status to `draft` on coinography.com (author unchanged).

---

## Mention rules

- Callback taps: no `@mention` needed.
- Reply to news card: `RATE`, `DRAFT`, `PUBLISH`, `EDIT` work without `@mention`.
- Reply to bot edit prompt with `.md`: works via `reply_to_bot` implicit mention.
