# Phase 1.5: News Card Inline URL Tiles

**Status:** implemented  
**Created:** 2026-05-23  
**Depends on:** Phase 1 (`PLANS/news-card-telegram.md`)

---

## Scope

Add Telegram **InlineKeyboard URL button tiles** under each news card:

- Row 1: `Read Article` → WordPress URL
- Row 2: `Google Doc` → Drive URL (only if present)

Caption HTML links removed; tiles replace them. `RATE / DRAFT / EDIT` remain plain text until Phase 2.

---

## Implementation

- `build_inline_keyboard()` in `build_and_send_card.py`
- `reply_markup` JSON passed to `sendPhoto` / `sendMessage`
- `inline_keyboard` rows saved in `publish/news-card.json`

---

## Phase 2 (implemented)

Rate callback tiles + image score menu. See `PLANS/news-card-telegram-feedback.md`.

Draft/Edit tiles deferred to Phase 3.

---

## Change log

| Date | Note |
|------|------|
| 2026-05-23 | Phase 1.5 implemented |
| 2026-05-23 | Phase 2 plan linked (rate tiles added in Phase 2 implementation) |
