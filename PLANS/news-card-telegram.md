# Phase 1: News Card + Telegram Group Send

**Status:** implemented  
**Created:** 2026-05-23  
**Scope:** Card builder + Telegram send only. No DB, no feedback, no buttons, no WhatsApp.

---

## Pipeline hook

After Step 6 WordPress publish succeeds and `cleanup_run_artifacts.sh` runs:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/build_and_send_card.py \
  --manifest "$PIPELINE_MANIFEST"
```

Fail-open: always exit 0; never blocks pipeline.

---

## Files

| File | Purpose |
|------|---------|
| `workspace-orchestrator/config/telegram_card_config.json` | Target Telegram group |
| `workspace-orchestrator/skills/pipeline/build_and_send_card.py` | Build card JSON + send photo/caption |
| `$RUN_DIR/publish/news-card.json` | Per-run card artifact |

---

## Card fields

See `build_and_send_card.py` — sourced from `validated.json`, `wordpress.json`, optional `google-drive.json`, `manifest.json`.

---

## Phase 1.5 (implemented)

Inline URL button tiles under each card: **Read Article**, **Google Doc** (if drive URL exists). See `PLANS/news-card-telegram-inline-buttons.md`.

---

## Phase 2 (implemented)

Article DB + RATE feedback. See `PLANS/news-card-telegram-feedback.md`.

DRAFT / EDIT → Phase 3 (`PLANS/news-card-telegram-draft-edit.md`, implemented).

---

## Change log

| Date | Note |
|------|------|
| 2026-05-23 | Phase 1 implemented |
| 2026-05-23 | Phase 1.5: inline URL tiles (Read Article, Google Doc) |
| 2026-05-23 | Phase 2 plan: editorial DB + RATE (see news-card-telegram-feedback.md) |
| 2026-05-23 | Phase 3: DRAFT + EDIT (see news-card-telegram-draft-edit.md) |
| 2026-05-23 | Phase 3b: Publish from Telegram (see news-card-telegram-publish.md) |
