# Phase 3: DRAFT + EDIT Editorial Feedback

**Status:** implemented  
**Created:** 2026-05-23  
**Depends on:** Phase 2 (`PLANS/news-card-telegram-feedback.md`)

---

## Scope

| Feature | Behavior |
|---------|----------|
| **Unpublish (DRAFT)** | Confirm → `POST /wp/v2/posts/{id}` status=draft |
| **Edit** | Send `.md` doc → user replies with corrected `.md` → confirm → apply to WP |
| **Versions** | `published_snapshot`, `user_suggested`, `applied` in `article_versions` |
| **Card UI** | Unpublish + Edit callback buttons |

---

## Files

| File | Purpose |
|------|---------|
| `editorial_db.py` | Extended schema: versions, edit_sessions, editorial_actions |
| `handle_card_feedback.py` | DRAFT + EDIT flows (extends Phase 2 RATE/IMAGE) |
| `wp_post_actions.sh` | WP set-status + update-content |
| `build_and_send_card.py` | Unpublish/Edit buttons, snapshot on send |

---

## Callback prefixes

| callback_data | Action |
|---------------|--------|
| `oc_draft:{run_id}` | Confirm unpublish |
| `oc_draft_yes:{run_id}` | Execute unpublish |
| `oc_draft_no:{run_id}` | Cancel |
| `oc_edit:{run_id}` | Start edit, send document |
| `oc_edit_apply:{run_id}` | Apply suggested edit to WP |
| `oc_edit_cancel:{run_id}` | Cancel edit session |

---

## Phase 3b (implemented)

Publish button on news card. See `PLANS/news-card-telegram-publish.md`.

---

## Change log

| Date | Note |
|------|------|
| 2026-05-23 | Phase 3 plan created |
| 2026-05-23 | Phase 3 implemented |
| 2026-05-23 | Phase 3b: Publish button implemented |
| 2026-05-23 | Edit safeguards: no-change rejection, diff summary on confirm, Publish button after apply on draft |
