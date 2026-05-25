# EDITORIAL_FEEDBACK.md — News Card Editorial Feedback

Handles Telegram feedback on published news cards. **Not part of the crypto pipeline.**

---

## When this applies

| Pattern | Example |
|---------|---------|
| Callback `oc_r:` / `oc_ri:` / `oc_ri_menu:` | Rate article or image |
| Callback `oc_draft:` / `oc_draft_yes:` / `oc_draft_no:` | Unpublish flow |
| Callback `oc_publish:` / `oc_publish_yes:` / `oc_publish_no:` | Publish flow |
| Callback `oc_edit:` / `oc_edit_apply:` / `oc_edit_cancel:` | Edit flow |
| Text | `RATE 8`, `IMAGE 7`, `DRAFT`, `PUBLISH`, `EDIT` |
| Document reply | `.md` file replying to bot's edit prompt |

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

---

## Pipeline vs feedback

| User says | Route |
|-----------|-------|
| `run pipeline`, `run crypto news pipeline` | SOUL.md pipeline |
| RATE, IMAGE, DRAFT, PUBLISH, EDIT, callback taps, edit `.md` upload | This doc |

Pipeline takes precedence only when the message explicitly requests the pipeline.

---

## PUBLISH flow notes

- Tap **Publish** or reply `PUBLISH` to the card.
- Confirm with **Yes, publish** / **Cancel**.
- Sets WordPress post status to `publish` (live on site).
- If already published → bot replies "already published".

---

## EDIT flow notes

- Tap **Edit** or reply `EDIT` to the news card.
- Bot sends `article-{run_id}.md` as a document.
- Download → edit → **save** → reply to that document with a corrected **`.md` file** (not pasted text).
- Identical upload (no changes) → bot replies "No changes detected" and keeps the edit session open (`EDIT_UNCHANGED`).
- Changed upload → bot shows **+N / −M lines** summary plus **Apply to WordPress** / **Cancel** buttons.
- After apply on a draft → bot offers a **Publish** button on the confirmation message.
- Pasted text during edit → handler returns `EDIT_USE_DOCUMENT`.

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
- Sets WordPress post status to `draft`.

---

## Mention rules

- Callback taps: no `@mention` needed.
- Reply to news card: `RATE`, `DRAFT`, `PUBLISH`, `EDIT` work without `@mention`.
- Reply to bot edit prompt with `.md`: works via `reply_to_bot` implicit mention.
