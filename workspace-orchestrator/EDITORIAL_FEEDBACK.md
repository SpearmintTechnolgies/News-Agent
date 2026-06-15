# EDITORIAL_FEEDBACK.md — News Card Editorial Feedback

Handles Telegram feedback on news cards sent after pipeline Step 6. **Part of the Step 6 success path** (card send) but **not** a pipeline step itself — do not spawn subagents for editorial actions.

---

## When this applies

| Pattern | Example |
|---------|---------|
| Callback `oc_r:` / `oc_ri:` / `oc_ri_menu:` | Rate article or image |
| Callback `oc_draft:` / `oc_draft_yes:` / `oc_draft_no:` | Unpublish flow |
| Callback `oc_publish:` / `oc_pub_a:` / `oc_pub_y:` / `oc_pub_n:` | Publish flow (author picker) |
| Callback `oc_edit:` / `oc_edit_apply:` / `oc_edit_cancel:` | Edit flow |
| Callback `oc_sel:` / `oc_feed_refresh:` | Feed card: toggle selection / refresh (handler does everything) |
| Callback `oc_go:` | Feed card: publish selected — handler writes a selection file, THEN you start the pipeline (see below) |
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
5. If handler failed before replying, show the stdout line to the user.

### Feed-card selection (`oc_sel` / `oc_feed_refresh` / `oc_go`)

These come from the daily headline feed card (see `send_feed_card.py`). Run the same handler with `--payload`.

- `oc_sel:` (toggle) and `oc_feed_refresh:` (refresh) are **fully handled** by `handle_card_feedback.py` (it edits the card in place). It prints `FEED_SELECTED` / `FEED_CARD_REFRESHED`. End your turn — do NOT start the pipeline.
- `oc_go:` (Publish selected) is the **one exception that starts the pipeline**. Run the handler first; it marks the chosen pool stories `selected`, posts "Starting pipeline for N selected stories…" to the group, and prints one of:
  - `FEED_GO_EMPTY: <feed_id>` → nothing selected; the handler already told the user. End turn.
  - `FEED_GO: project=<slug> feed_id=<id> count=<N> selection_file=<path>` → now switch to **SOUL.md → Selected-stories entry**: set `PROJECT_SLUG`, `SELECTION_FILE`, `PICKER_MODE=classify-only`, `STEP25_GATE=ON`, `N=<count>`, and run the pipeline (Step 0 → Step 1 classify-only → Step 1c → Step 2 queue → Step 3).

---

## Pipeline vs feedback

| User says / incoming | Route |
|-----------|-------|
| `run pipeline`, `run crypto news pipeline` | SOUL.md pipeline (pool-backed) |
| `oc_go:` feed-card publish | This doc → then SOUL.md Selected-stories entry |
| `AUTO_RUN ...` (idle watchdog cron) | SOUL.md Auto-run entry |
| "fetch/latest/refresh news" | SOUL.md Refresh-feed entry (`send_feed_card.py`) |
| RATE, IMAGE, DRAFT, PUBLISH, EDIT, `oc_sel`/`oc_feed_refresh` taps, edit `.md` upload | This doc |

Pipeline takes precedence only when the message explicitly requests the pipeline.

**Channels:** Pipeline gate (Step 5 yes/no) is in the **Telegram DM** with Nexus. News cards and editorial feedback run in the **`news-agent` group** (`config/telegram_card_config.json`). RATE/PUBLISH/EDIT in DM will not resolve articles unless you reply to the card in the group.

---

## PUBLISH flow notes

- Tap **Publish** or reply `PUBLISH` to the card.
- Bot shows **Toby** / **Ahmed** / **Golan** author buttons (`config/wp_authors.json`).
- Pick author → confirm **Yes, publish as …** / **Cancel**.
- WordPress receives `status: publish` + `author: 3|17` on **coinography.com** via `wp_post_actions.sh`.
- If already published → bot replies "already published".
- Draft posts are created by the pipeline as author renu (API user); byline changes only at Telegram publish.

### Callback prefixes

| callback_data | Action |
|---------------|--------|
| `oc_publish:{run_id}` | Show author picker |
| `oc_pub_a:{run_id}:{author_id}` | Confirm chosen author |
| `oc_pub_y:{run_id}:{author_id}` | Publish live with author |
| `oc_pub_n:{run_id}` | Cancel publish flow |

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
