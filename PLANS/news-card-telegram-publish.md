# Phase 3b: Publish from Telegram

**Status:** implemented  
**Created:** 2026-05-23  
**Depends on:** Phase 3 (`PLANS/news-card-telegram-draft-edit.md`)

---

## Scope

| Feature | Behavior |
|---------|----------|
| **Publish button** | Confirm → `POST /wp/v2/posts/{id}` status=publish |
| **Text command** | Reply `PUBLISH` to card |
| **Already live** | "Already published" — no WP call |
| **Card UI** | `[Unpublish] [Publish] [Edit]` on one row |

Closes the draft ↔ live loop started in Phase 3.

---

## Callback prefixes

| callback_data | Action |
|---------------|--------|
| `oc_publish:{run_id}` | Confirm publish |
| `oc_publish_yes:{run_id}` | Execute publish |
| `oc_publish_no:{run_id}` | Cancel |

---

## Change log

| Date | Note |
|------|------|
| 2026-05-23 | Phase 3b plan created |
| 2026-05-23 | Phase 3b implemented |
