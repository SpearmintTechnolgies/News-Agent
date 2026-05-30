# BACKLINK_FEEDBACK.md — Backlink Approval Card Feedback

Handles Telegram feedback on backlink approval cards. **Not** a full pipeline step — run the Python handler directly. **Do not spawn subagents.**

---

## When this applies

| Pattern | Example |
|---------|---------|
| Callback `bl_approve:` / `bl_reject:` / `bl_edit:` | Inline button taps |
| Text | `APPROVE`, `REJECT`, `EDIT` |
| Reply text after Edit | User's edit instructions (reply to card) |

---

## What to do

1. **Do NOT** start the crypto news pipeline.
2. **Do NOT** spawn subagents for card feedback.
3. Run the handler immediately:

```bash
python3 ~/.openclaw/workspace-orchestrator-backlink/tools/telegram/handle_backlink_callback.py \
  --callback-data "<callback data if present>" \
  --text "<user text if present>" \
  --user-id "<telegram user id>" \
  --chat-id "<telegram chat id>" \
  --reply-to-message-id "<replied-to message id if present>" \
  --db ~/.openclaw/data/backlink_agent.db
```

4. **If the handler already sent a Telegram reply, end turn silently** — do not repeat stdout in the group.
5. If the handler failed before replying, show the error.

---

## Callback prefixes (Phase 1)

| callback_data | Action |
|---------------|--------|
| `bl_approve:{workflow_id}` | Approve → `APPROVED` (ready-to-publish queue; **no auto-publish**) |
| `bl_reject:{workflow_id}` | Reject → `REJECTED` / archived |
| `bl_edit:{workflow_id}` | Request edit → `EDIT_REQUESTED`, wait for instructions |

After **Edit**, the user's next message with edit instructions triggers content revision. Then follow **SOUL.md Edit loop** — spawn **bl-content** only, re-validate, re-send card.

---

## Pipeline vs feedback

| User says | Route |
|-----------|-------|
| `run backlink pipeline`, `discover backlink`, `batch discover` (no URL) | SOUL.md — **default batch discover** |
| `run backlink pipeline for https://…` (URL in message) | SOUL.md — single-URL pipeline |
| Approve / Reject / Edit buttons, callback taps, edit reply text | This doc |

---

## Channels

- Backlink cards and feedback run in the **backlinks-agent** group (`config/telegram_backlink_config.json`, id `-5291081154`).
- Crypto news cards use `oc_*` prefixes in the **news-agent** group — route via `EDITORIAL_FEEDBACK.md` in the crypto orchestrator workspace.

---

## Phase 1 — after approve

When state is `APPROVED`, **stop**. Do not run publisher or verifier. The opportunity is in the ready-to-publish queue for manual Phase 2.

Do **not** call:

```bash
python3 workflows/workflow_driver_cli.py run --id <WORKFLOW_ID> ...
```

after approve unless Phase 2 publish is explicitly enabled in config.
