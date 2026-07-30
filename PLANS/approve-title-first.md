# Approve-Title-First News Flow

**Status:** implemented
**Created:** 2026-06-13

Inverts the news pipeline from "auto-pick + approve-at-the-end" to "approve-the-title-first": a standalone 24/7 scanner fills a per-project headline pool, a daily 10:00 card lets the team pick titles up front, the pipeline runs only on chosen titles, and an idle watchdog auto-publishes (capped 4/day) after 48h of group silence. Any story that fails automatically is backfilled from the pool so the batch still hits its target.

---

## Components

### 1. 24/7 scanner -> per-project pool (no LLM)
- `update_headline_pool.py [--all]` wraps the deterministic `scan_headlines.run_scan` and upserts survivors into `editorial.db` `headline_pool` (PK `(project, url)`).
- STRICTLY project-scoped: every pool helper requires an explicit `project` (no cross-project read path). The two project lists can never mix.
- Scheduled as an OpenClaw cron `--command` job (~30m) -> zero tokens.

### 2. Daily feed card (no LLM)
- `send_feed_card.py [--all]` posts the top ~10 fresh pool headlines per project to the news-agent group with inline buttons:
  - `oc_sel:{feed_id}:{idx}` toggle (edits the card in place via `editMessageReplyMarkup`)
  - `oc_go:{feed_id}` publish selected
  - `oc_feed_refresh:{feed_id}` rebuild from the latest pool
- Records a `feed_cards` row. Scheduled by `pool_scheduler.py` at 10:00 local.

### 3. Selection -> pipeline (classify-only)
- `handle_card_feedback.py` handles `oc_sel`/`oc_feed_refresh` fully; `oc_go` writes `/tmp/<slug>-...` selection file + prints `FEED_GO:` for the orchestrator.
- `build_picker_input.py --from-pool --selection-file ... --classify-only` keeps ALL selected stories; `validate_picks.py --classify-only` skips diversity drop. Picker only labels categories. Then the existing per-pick pipeline runs (Step 2.5 gate kept, in the group).

### 4. 48h idle auto-run (cron-only, NO heartbeat)
- `check_auto_run.py` (cron `--command`, hourly, zero tokens): if `now - last_contact_at >= 48h` and a project has quota (`published_today < 4`) + fresh pool, it wakes the orchestrator ONCE via a one-shot `openclaw cron add --agent orchestrator --message "AUTO_RUN ..."`.
- `last_contact_at` (group-level, in `pipeline_state`) is stamped on any engagement by `handle_card_feedback.py`. Daily-while-silent falls out of the daily cap.
- Auto path: picker SELECTS with diversity, Step 2.5 gate skipped, runs both projects one by one.

### 5. Backfill on automatic failure
- On any automatic failure (topic_duplicate / research / write / image), the orchestrator runs `get_backfill_candidate.py` -> classify via picker -> `validate_picks.py --classify-only --append-to picks.json` (new `pick_index` appended) -> pushes the replacement onto the queue. Bounded by `MAX_BACKFILLS`. Human `no`/`stop` is never backfilled.

---

## DB additions (`editorial_db.py`)
- `headline_pool(project, url, headline, source, pub_date, summary, corroborating_json, scanned_at, status)` PK `(project, url)`; status `fresh|shown|selected|consumed`.
- `feed_cards(feed_id, project, telegram_group, telegram_message_id, candidates_json, selected_json, status, ...)`.
- `pipeline_state(project, key, value, updated_at)` PK `(project, key)` — `_global/last_contact_at`, `_global/last_auto_fire_at`.
- Helpers: `upsert_pool_candidates`, `fresh_pool`, `available_for_backfill`, `pool_by_urls`, `mark_pool`, `prune_pool`, feed-card CRUD, `set_state`/`get_state`, `touch_last_contact`, `hours_since_last_contact`, `published_today`.

---

## Cron jobs (3, all `--command`, zero tokens)
- scanner: `--every 30m` -> `update_headline_pool.py --all`
- feed card: daily 10:00 local -> `send_feed_card.py --all`
- idle watchdog: `--every 1h` -> `check_auto_run.py`

No heartbeat is used; OpenClaw's default heartbeat is untouched. No Telegram permission/config changes.

---

## Removed / changed
- In-pipeline HEADLINE_SCAN spawn removed from orchestrator SOUL; candidates always come from the pool. `scan_headlines.py` remains the scanner engine.
- Step 2 loop is now a refillable queue (TARGET/PUBLISHED) with backfill. Step 2.5 gate is conditional (`STEP25_GATE`) and lives in the group.

## Change log
| Date | Note |
|------|------|
| 2026-06-13 | Implemented: pool, feed card, selection routing, classify-only + backfill, 48h cron watchdog, SOUL entries. |
