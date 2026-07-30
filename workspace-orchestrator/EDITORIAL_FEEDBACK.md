# EDITORIAL_FEEDBACK.md — News Card Editorial Feedback

Handles Telegram feedback on news cards sent after pipeline execution.
**DO NOT** start the pipeline or spawn subagents when handling editorial feedback.

---

## When This Applies

| Incoming Callback / Text | Action |
|--------------------------|--------|
| `oc_r:`, `oc_ri:`, `oc_ri_menu:`, `RATE 8`, `IMAGE 7` | Rate article or feature image |
| `oc_draft:`, `oc_draft_yes:`, `oc_draft_no:`, `DRAFT` | Unpublish flow (switch status to draft) |
| `oc_publish:`, `oc_pub_a:`, `oc_pub_y:`, `oc_pub_n:`, `PUBLISH` | Publish flow (in-card author picker: Toby/Ahmed/Golan) |
| `oc_edit:`, `oc_edit_apply:`, `oc_edit_cancel:`, `EDIT` | Edit flow (download `.md` -> upload edited `.md` -> apply) |
| `oc_go:`, `oc_feed_refresh:` | Feed card callbacks (handled by `feed-tap-claimer` or handler) |
| Document reply (`.md` file) | Apply edited Markdown file to WordPress post |

---

## Execution Protocol

Run `handle_card_feedback.py` immediately in `bash`:

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

### Output Rules
- **Silent Turn:** If the handler sent a Telegram reply directly, OR if stdout is `NO_REPLY`, **end your turn silently with NO output text**.
- **Error Handling:** If the handler prints an explicit error line (not `NO_REPLY`), output that line.

---

## Callback Mechanics Summary

### 1. Feed-Card Taps (`oc_go:`, `oc_feed_refresh:`)
- Intercepted by `feed-tap-claimer` plugin in-process zero-LLM.
- Fallback in orchestrator: Run `handle_card_feedback.py`.
- `oc_go:` enqueues job in `feed_jobs` DB table and edits card. End turn (drainer runs pipeline asynchronously).

### 2. PUBLISH Flow
- `oc_publish:{run_id}` $\rightarrow$ Shows author buttons (Toby: 3, Ahmed: 17, Golan: 8).
- `oc_pub_a:{run_id}:{author_id}` $\rightarrow$ Publishes post live on WordPress immediately with chosen author byline.

### 3. EDIT Flow
- User replies `EDIT` $\rightarrow$ Handler sends `article-{run_id}.md`.
- User uploads updated `.md` file $\rightarrow$ Handler compares diff (+N / −M lines) and presents **Apply** / **Cancel** buttons.
- **CRITICAL:** Pass `--document-file-id` of the user's uploaded document from message metadata.
