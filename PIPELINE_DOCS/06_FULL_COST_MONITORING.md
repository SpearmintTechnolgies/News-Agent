# Full Cost Monitoring

This document extends Sawan’s original `PIPELINE_DOCS` set (`00`–`05`). Same pipeline, same agents, same run-bundle. The only addition sir asked for: **every story must report the full cost**, not image-only.

Sawan already built the meter. We must **use it the way he designed**, not bypass it.

---

## 🌟 The Rule (Sawan’s approach)

Nexus does **not** write, research, or draw. It **spawns** worker agents. Those sessions are the only place tokens get attributed.

| Allowed (costs are recorded) | Forbidden (cost goes missing) |
|------------------------------|-------------------------------|
| Nexus `sessions_spawn` → picker / researcher / writer / creator | Writing the article in Cursor / ChatGPT |
| Deterministic scripts in bash (`publish.sh`, `finalize_story.py`, validators) | Orchestrator LLM inventing publish authors / narrating callbacks |
| `finalize_story.py` → `aggregate_run_tokens.py` → `publish/tokens.json` | Reporting `image-cost.json` alone as “the story cost” |

**COST RULE from `SOUL.md`:** run scripts in bash. Never spawn `publisher` or `wp-publisher`. Only spawn `researcher`, `picker`, `writer`, `creator` (optional `chart-generator`).

If a human or Cursor does Scout/Quill’s job, `tokens.json` will show **$0 text + one image**. That is a **broken attribution**, not a cheap story.

Cursor is **forbidden** from substituting Nexus/Scout/Quill/Pixel (see `.cursor/rules/sawan-pipeline.mdc`). If FEED_DRAIN dies, fix the drain — do not finish the story in chat.

---

## 🎯 What “full cost” means

Full cost = sum of every billed call that produced **that run_id**:

1. **Picker (Sieve)** — classify / select  
2. **Researcher (Scout)** — deep research  
3. **Writer (Quill)** — article + any rewrite loops  
4. **Creator (Pixel)** — image prompt  
5. **Orchestrator (Nexus)** — control loop for that run window  
6. **Image API** — Nano Banana / listed model (`image-model-pricing.json`)  
7. **Editorial feedback** — RATE / PUBLISH / EDIT Nexus turns (add-on; currently after finalize)

**Not LLM cost (Sawan kept these script-only):** RSS pool scan, `pool_scheduler.py`, `dispatch_feed_jobs.py`, `publish.sh`, `wp_post_actions.sh`, Drive `gog` upload.

---

## 🛠️ How Sawan wired the meter

After WordPress draft, Nexus runs:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/pipeline/finalize_story.py \
  --manifest "$PIPELINE_MANIFEST" \
  --pick-id $PICK_ID \
  --drive-upload
```

`finalize_story.py` calls `aggregate_run_tokens.py`, which:

- Scans `~/.openclaw/agents/{researcher,writer,creator,chart-generator,picker,orchestrator}/sessions/`
- Keeps only logs that **reference the run dir**
- Time-slices orchestrator to the iteration window
- Prorates picker if batch `target_count` > 1
- Prices text from `openclaw.json` model catalog
- Adds `RUN_DIR/publish/image-cost.json`

**Outputs (system of record):**

| Artifact | Path |
|----------|------|
| Token + USD rollup | `$RUN_DIR/publish/tokens.json` |
| Image line item | `$RUN_DIR/publish/image-cost.json` |
| DB copy | `editorial.db` → `articles.cost_usd`, `tokens_total` |

Do not invent a second spreadsheet. Read these files.

---

## 🚀 How a story is supposed to run (so cost is complete)

Same runbook as `04_RUNBOOK_AND_TROUBLESHOOTING.md`, current Windows + Telegram + WordPress shape:

1. Gateway up. Docker `openclaw-news-agent` **stopped** (do not steal Telegram `getUpdates`). Vertex proxy may stay up.
2. Trigger **only** via Sawan’s entries: Telegram `oc_go` / `FEED_DRAIN`, or `run pipeline` to Nexus — **not** Cursor writing.
3. Nexus: `feed_drain_first.sh` or `init_run.sh` → spawn **picker** → per pick spawn **researcher** → **writer** → **creator** → bash `publish.sh` (draft) → `finalize_story.py`.
4. Human review on Telegram card. Publish = `handle_card_feedback.py` (`oc_publish` / `oc_pub_a`). No LLM author-name guessing.
5. **Then** open `tokens.json` and report full cost (USD + INR).

### After every story — cost checklist

1. `tokens.json` has **non-zero** `tokens_in` / `tokens_out`.  
2. `by_agent` includes at least researcher, writer, creator, orchestrator (picker if used).  
3. `image_cost_usd` present.  
4. `editorial.db` article row matches.  
5. If publish buttons were used, add orchestrator usage after finalize (same `run_id`).  
6. Convert at the day’s USD/INR. Quote **pipeline recorded** and **extras** separately.

If `tokens_total` is 0 and only image cost exists → say **“attribution failed; not full cost”**. Do not send ₹0.003 as the story bill.

---

## ⚠️ Cost gotchas (same class as Sawan’s runbook errors)

### 1. Cursor / human did Scout or Quill
- **Where:** research/write files appear in `$RUN_DIR` with no matching agent session.  
- **Symptom:** `tokens_total: 0`, only `image_cost_usd`.  
- **Fix:** Re-run that story through Nexus workers. Do not “finish the article” in Cursor if the goal is a costed production run.

### 2. `vertex-credits` catalog priced at `$0`
- **Where:** `openclaw.json` → `models.providers.vertex-credits`.  
- **Symptom:** credits are consumed; USD field still 0.  
- **Fix:** For sir’s INR report, apply Vertex **list** prices (Flash ~$0.10 / 1M in, ~$0.40 / 1M out) **or** switch the run to `local-bifrost` models that already have non-zero `cost` in catalog.

### 3. FEED_DRAIN died (WSL `bash`, empty Vertex, cron `--delete-after-run`)
- **Where:** job stays `running`; workers never log against the run dir.  
- **Fix:** Git Bash only; `feed_drain_first.sh`; do not leave Docker `openclaw-news-agent` polling the same bot.

### 4. Editorial LLM chatter
- **Where:** Nexus talks about `oc_pub_a` instead of running `handle_card_feedback.py` once.  
- **Symptom:** extra Flash tokens, HTML / `chat not found` noise, still no extra line in `tokens.json`.  
- **Fix:** `EDITORIAL_FEEDBACK.md` — one exec, silent turn.

### 5. Reporting image as full cost
- **Where:** reading `image-cost.json` only.  
- **Fix:** always start from `tokens.json` `cost_usd` + `by_agent`.

---

## 📊 Reference band (fully automated OpenClaw runs)

From `editorial.db` (Sawan-style runs, late Jul 2026):

| Band | USD | INR @ ₹95.4 |
|------|-----|-------------|
| Typical full story | $0.28–$0.70 | **₹27–₹67** |
| Image only (Nano Banana Lite) | $0.000034 | **₹0.003** |

**12 Aug 2026 Cardano (`20260812-174854-1273-27067`):** recorded **$0.000034 / ₹0.003** because research/write were **not** done by Scout/Quill sessions. That run did **not** follow Sawan’s approach. Next production stories must.

---

## 📁 Files (additions to `05_FILE_MANIFEST.md`)

| File | Role |
|------|------|
| `workspace-orchestrator/skills/pipeline/aggregate_run_tokens.py` | Sums agent sessions + prices |
| `workspace-orchestrator/skills/pipeline/finalize_story.py` | Calls aggregator at end of story |
| `workspace-orchestrator/config/image-model-pricing.json` | Per-image USD |
| `$RUN_DIR/publish/tokens.json` | **Official full cost** |
| `$RUN_DIR/publish/image-cost.json` | Image line only |
| `~/.openclaw/data/editorial.db` | Persisted `cost_usd` / tokens |
| `~/.openclaw/openclaw.json` | Text-model USD catalog |

---

*Sawan’s structure: agents + scripts + run-bundle. Full cost is a readout of that structure — it only works if we run the pipeline his way.*
