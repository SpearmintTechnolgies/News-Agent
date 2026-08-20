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

Run the wrapper immediately — **one exec, then stop**. Do not interpret `callback_data`, invent author names, narrate parsing, or type `C:\Users\...`.

```text
C:\tmp\oc-feedback.cmd --payload <callback> --chat-id -1003736953686 --user-id <id> --username <name>
```

`--chat-id` must be the numeric group id (`-1003736953686`). Strip any `telegram:` prefix.
Pass the callback string **exactly** as received (e.g. `oc_pub_a:20260812-174854-1273-27067:1`). Coinnetwork authors are configured in project JSON (Renu Sharma / Marcus Webb) — never invent other names.

### Output Rules
- **Silent Turn:** If the handler sent a Telegram reply directly, OR if stdout is `NO_REPLY` / `FEED_JOB_ENQUEUED` / `*_OK` / `*_SENT` / `PUBLISH_*`, **end your turn with zero text** (literally `NO_REPLY` only). Never narrate “I clicked a card”, “waiting for script”, or path/debug talk in the group. That work is background.
- **Error Handling:** Do **not** paste stack traces or UnicodeDecodeError into Telegram. Log locally. Group stays quiet.

---

## Callback Mechanics Summary

### 1. Feed-Card Taps (`oc_go:`, `oc_feed_refresh:`)
- Intercepted by `feed-tap-claimer` plugin in-process zero-LLM.
- Fallback in orchestrator: Run `handle_card_feedback.py`.
- `oc_go:` enqueues job in `feed_jobs` DB table and edits card. End turn (drainer runs pipeline asynchronously).

### 2. PUBLISH Flow
- `oc_publish:{run_id}` $\rightarrow$ Shows author buttons from project config (Coinnetwork: Renu Sharma / Marcus Webb).
- `oc_pub_a:{run_id}:{author_id}` $\rightarrow$ Publishes post live on WordPress immediately with chosen author byline.

### 3. EDIT Flow
- User replies `EDIT` $\rightarrow$ Handler sends `article-{run_id}.md`.
- User uploads updated `.md` file $\rightarrow$ Handler compares diff (+N / −M lines) and presents **Apply** / **Cancel** buttons.
- **CRITICAL:** Pass `--document-file-id` of the user's uploaded document from message metadata.
