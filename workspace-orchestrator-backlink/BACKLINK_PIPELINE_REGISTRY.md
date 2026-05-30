# Backlink Pipeline Registry

Single reference for the cryptography.com backlink pipeline in `workspace-orchestrator-backlink/`.

**DB:** `~/.openclaw/data/backlink_agent.db`  
**Telegram bot:** `@backlinks_agent_bot` → group `backlinks-agent` (`-5291081154`)  
**OpenClaw agent:** `orchestrator-backlink` (+ `bl-*` workers, all on `gemini-3.5-flash` during testing)  
**Phase:** `config/phase.json` → `"phase": 1` (approval queue only; publish parked for Phase 2)

---

## Architecture (Phase 1 — intelligent orchestrator)

**Brain:** `orchestrator-backlink` spawns workers, validates each step, retries on failure.  
**Workers:** `bl-discovery` → `bl-scorer` → `bl-auditor` → `bl-content` (each has own workspace under `workers/`).  
**Fallback:** `config/orchestrator.json` `"mode": "driver"` uses `workflow_driver_cli.py run` directly.  
**Run bundle:** `~/.openclaw/data/backlink_runs/{WF-ID}/manifest.json` via `scripts/init_workflow_run.sh`.

```text
Telegram (@backlinks_agent_bot) → orchestrator-backlink
  → sessions_spawn bl-worker → sessions_yield
  → workflows/validators/verify_workflow_step.py
  → send_backlink_card.py (approval)
Callbacks (Approve/Edit/Reject) → handle_backlink_callback.py (no spawn)
```

```text
Niche (crypto)
  └── Project (cryptography.com)
        └── Opportunity (unique url_hash per project)
              └── Workflow (WF-YYYYMMDD-HHMMSS)
```

```text
NEW → DISCOVERED → SCORED → AUDITED → CONTENT_READY
  → PENDING_APPROVAL → APPROVED (queue)
```

Side paths: `REJECTED`, `APPROVAL_EXPIRED`, `ARCHIVED`, `FAILED`  
Edit loop: `EDIT_REQUESTED` → `content` → new card

Phase 2 (parked): full publish/verify chain via `BACKLINK_PHASE=2` or `config/phase.json` `"phase": 2`

---

## Telegram triggers (default: batch discover)

| Message | Behavior |
|---------|----------|
| `@backlinks_agent_bot run backlink pipeline` | **Default** — search for new sites, pipeline each |
| `@backlinks_agent_bot batch discover` | Same as above |
| `@backlinks_agent_bot run backlink pipeline for https://…` | Single URL only (skip search) |

Search queries come from `config/campaign.json` → `search_queries` (overridable via `discover --queries`).

---

## CLI (`workflows/workflow_driver_cli.py`)

| Command | Purpose |
|---------|---------|
| `bootstrap` | Create campaign + opportunity + workflow |
| `run --id WF-...` | Run automated steps until wait state |
| `step --id WF-...` | Run one step |
| `status --id WF-...` | Workflow summary + logs |
| `list` | All workflows in DB |
| `discover` | Search → new opportunities |
| `callback --callback-data bl_approve:WF-...` | Simulate Telegram button |
| `report` | SQL learning / pipeline report |
| `expire` | Expire stale `PENDING_APPROVAL` (>48h) |

Global flag: `--db PATH` (after subcommand).

---

## Skills (`skills/pipeline/`)

| Skill | Phase 1 step | Role |
|-------|--------------|------|
| `discover_opportunities.py` | Discovery | Search, fetch, parse, dedup |
| `score_candidate.py` | Scoring | Qualify + blacklist + learning weights |
| `audit_opportunity.py` | Audit | Placement scan, image_required, pass/fail |
| `generate_content.py` | Content | Text + **mandatory** feature image via `skills/generate-image/generate.sh` |
| `validate_image.py` | Content | JPEG/size validation (news pipeline pattern) |
| `generate_draft.py` | Legacy | Template drafts (kept for Phase 2 / fallback) |
| `detect_placement.py` | Audit helper | guest_post / comment / form signals |
| `publish_backlink.py` | Phase 2 | Playwright submit (dry-run default) |
| `verify_backlink.py` | Phase 2 | Confirm target link visible |
| `update_learning_weights.py` | Feedback | Record approve/reject/edit + verified outcomes |

---

## Tools (`tools/`)

| Tool | Role |
|------|------|
| `search/search.py` | Web search via ddgs (`google` → `bing` → `auto` → `brave`; gate: `scripts/test_search_backends.py`) |
| `page_fetch/page_fetch.py` | Playwright HTML fetch |
| `parser/parser.py` | Forms, signals, guest-post detection |
| `telegram/send_backlink_card.py` | Rich card (project/audit/content/image) + sendPhoto |
| `telegram/handle_backlink_callback.py` | Approve / Edit / Reject + feedback_events |

---

## Config

| File | Purpose |
|------|---------|
| `config/phase.json` | `1` = stop at APPROVED; `2` = full publish chain |
| `config/campaign.json` | Legacy target site seed (migrates to projects) |
| `config/telegram_backlink_config.json` | Bot token, group id |
| `config/publish_config.json` | `dry_run_default` for publish (Phase 2) |
| `config/orchestrator.json` | `intelligent` vs `driver` mode, retry count, `default_trigger: batch_discover` |
| `config/learning_weights.json` | Auto-updated domain boost/penalty |
| `config/agents_testing.json` | Flash model list (testing phase) |

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/test_search_backends.py` | Search gate — must pass before batch discover |
| `scripts/init_workflow_run.sh` | Create run bundle + optional URL bootstrap |
| `scripts/update_manifest_step.sh` | Update manifest step status |
| `scripts/read_manifest.sh` | Print manifest summary |
| `scripts/allocate_workflow_id.py` | Collision-safe WF-ID |

---

## Validators (`workflows/validators/`)

| Script | Step |
|--------|------|
| `validate_discovery.py` | discover |
| `validate_score.py` | score |
| `validate_audit.py` | audit |
| `validate_content.py` | content |
| `verify_workflow_step.py` | Unified `--step` entry |

---

## Database (v3 tables)

| Table | Purpose |
|-------|---------|
| `niches` | Vertical (e.g. crypto) |
| `projects` | Promoted site per niche |
| `audits` | Scanner output per workflow |
| `content_assets` | Versioned content + image paths |
| `feedback_events` | Approve / edit / reject for learning |

Legacy tables (`campaigns`, `drafts`, `approvals`, etc.) remain for compatibility.

---

## Telegram card (Phase 1)

**Sections:** niche, project, target URL, opportunity site, score, audit summary, content preview, image path  
**Buttons:** Approve | Edit | Reject (no Blacklist in UI)  
**Image:** `sendPhoto` with feature image — **required**; card will not send without it  
**Expiry:** 48 hours

---

## Image generation

Copied from news pipeline (not the creator agent):

```bash
bash skills/generate-image/generate.sh "<prompt>"
```

Per-workflow output: `~/.openclaw/data/backlink_media/{workflow_id}/feature.jpg`  

**Mandatory:** every workflow attempts feature image generation (3 tries). If all fail, workflow **continues** and the card shows a short image-failure note (text-only, no photo).  
Tests only: `BACKLINK_SKIP_IMAGE_GEN=1` writes a placeholder JPEG.

---

## OpenClaw agents

| Agent | Workspace | Role |
|-------|-----------|------|
| `orchestrator-backlink` | `workspace-orchestrator-backlink/` | Spawn, validate, retry, send card |
| `bl-discovery` | `workers/bl-discovery/` | Discovery step |
| `bl-scorer` | `workers/bl-scorer/` | Scoring step |
| `bl-auditor` | `workers/bl-auditor/` | Audit step |
| `bl-content` | `workers/bl-content/` | Content + image step |
| `bl-publisher` / `bl-verifier` | legacy workspace | Phase 2 only |

Restart gateway after `openclaw.json` changes.

---

## Tests

```bash
cd workspace-orchestrator-backlink
python3 -m unittest discover -s tests -p 'test_*.py' -q
```

77 tests. Phase 2 publish/verify tests set `BACKLINK_PHASE=2`.  
Live search gate: `python3 scripts/test_search_backends.py`
