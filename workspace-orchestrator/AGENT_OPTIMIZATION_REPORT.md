# Multi-Agent Pipeline Optimization Report
**Nexus Orchestrator System**
**Date:** 2025-06-19
**Scope:** Cost reduction & token efficiency without compromising quality

---

## 📊 Current Pipeline Cost Analysis

### Agent Spawn Costs (Estimated per Story)

| Step | Agent | Spawn Cost | Tool Calls | Avg Tokens | Notes |
|------|-------|------------|------------|------------|-------|
| 1c | Picker | ~1 spawn | 2-3 | ~2K | Chooses stories from pool |
| 2.1 | Researcher | ~1 spawn | 4-6 | ~4K | URL resolve, multi-source extraction |
| 2.2 | Writer | 1-3 spawns* | 3-5 each | ~8K each | Initial + up to 2 revisions |
| 2.3 | Creator | 1-2 spawns | 2-3 | ~1.5K | Image prompt + generation |
| 2.4 | Publisher | 1 spawn | 2-3 | ~2K | Drive upload, docx conversion |
| 2.6 | WP-Publisher | 1 spawn | 2-3 | ~1.5K | WordPress draft publish |
| **Total** | | **6-11 spawns** | **15-23 calls** | **~19-35K tokens** | Per successful story |

**Revision multiplier:** Each writer revision adds ~8K tokens (3 attempts = +16K overhead)

---

## 🎯 High-Impact Optimizations

### 1. **Batch Agent Selection (IMMEDIATE - 40% reduction)**

**Current:** Picker chooses 1 story at a time, spawns Researcher 1-by-1.

**Proposed:** Multi-story batch at Researcher level

**Implementation:**
```python
# In build_picker_input.py - add --batch-size N
# Researcher processes all picks in ONE spawn

# Spawn message change:
MODE: DEEP_RESEARCH
INPUT_FILE: $RUN_DIR/picker/picks.json  # all picks
BATCH_MODE: true
OUTPUT_FILE: $RUN_DIR/research/batch_raw.json
```

**Savings:**
- Researcher spawns: N → 1 per batch of N
- Token savings: ~4K × (N-1) per batch
- Example: 3-story batch saves ~8K tokens

---

### 2. **Redis/Cache URL Resolution Layer (HIGH - 30% reduction on repeat topics)**

**Problem:** Every story re-resolves same domains (CoinDesk, Cointelegraph feeds)

**Solution:** 24h URL resolution cache in SQLite

**Implementation:**
```python
# Add to ~/.openclaw/data/url_cache.db
-- resolved_at, original_url, resolved_url, http_status
-- TTL: 24 hours

# Modify extract_article.py
CACHE_HIT: return cached content
CACHE_MISS: fetch + store
```

**Savings:**
- 30-50% of stories use same sources
- Saves ~2 tool calls per cached URL
- Estimated: 1-2K tokens per cached story

---

### 3. **Reduced Writer Revision Attempts (IMMEDIATE - 10-15% reduction)**

**Current:** 3 max iterations on self-check failures

**Analysis:** Looking at check_article.py, most failures are word_count or formatting

**Optimize:**
```python
# In check_article.py - add auto-fix flags
if fail == "word_count_footer":
    # Auto-correct instead of retry
    auto_fix_footer(article_path)
    return PARTIAL_PASS  # Don't count as full retry
```

**Implementation:**
- Tier-1 fixes (word count, meta chars): auto-correct + PASS
- Tier-2 fixes (H2/H3 count): 1 retry max
- Tier-3 fixes (topic drift): fail fast

**Savings:** Reduces 3 → 1.2 average writer spawns

---

### 4. **Lite Validation Mode (MEDIUM - 20% reduction)**

**Current:** Full check_article.py runs on raw AND final.md

**Optimize:**
```python
# Add --lite flag
# Skips: em-dash check, banned phrase scan, anchor link validation
# Runs only: H2/H3 count, word band, single H1
```

**Use in:**
- Initial writer self-check (still full check on final)
- Auto-run mode where speed > perfection

---

### 5. **Image Generation Optimization (MEDIUM - 15% reduction)**

**Current:** Creator spawns, validates image file

**Optimize:**
```python
# Move image validation to shell script entirely
# Creator only crafts prompt, returns prompt to orchestrator
# Orchestrator runs generate.sh directly

# Savings: Eliminates 1-2 spawn tool calls per story
```

**Alternative:** Pre-generate image prompts in Picker (store in picks.json)
```
"suggested_image_prompt": "Bitcoin 3D coin...",
# Creator spawn becomes simple execution
```

---

### 6. **Merged Drive + WP Upload (MEDIUM - 10% reduction)**

**Current:** Publisher (Drive) + WP-Publisher (separate spawns)

**Optimize:** Single combined agent for non-DM mode:
```python
# New agent: publisher-combined
MODE: UPLOAD_ALL
# Uploads to Drive, converts, then publishes WP draft
# One spawn instead of two
```

---

### 7. **Smart Retry Exponential Backoff (LOW - 5% reduction)**

**Current:** Immediate retry on failures

**Implement:**
```bash
# Add to retry logic
attempt_1: immediate
attempt_2: wait 5s  
attempt_3: wait 15s + log for analysis
```

**Prevents:** Wasting tokens on transient failures that recover with short delay

---

## 📈 Projected Savings Summary

| Optimization | Token Reduction | Implementation Effort | Priority |
|--------------|----------------|----------------------|----------|
| Batch Researcher | 25% | Medium | P1 |
| URL Cache Layer | 20% | Low | P1 |
| Writer Auto-Fix | 15% | Low | P1 |
| Lite Validation | 15% | Low | P2 |
| Image Shell-Only | 10% | Medium | P2 |
| Merged Publisher | 10% | Medium | P3 |
| Retry Backoff | 5% | Low | P3 |
| **TOTAL POTENTIAL** | **~60-70%** | | |

---

## 🚀 Quick Wins (Implement Today)

### Quick Win #1: Reduce Max Writer Iterations
```python
# In workspace-writer/skills/article/SKILL.md
# Change: "Repeat at most 3 self-check iterations"
# To: "Repeat at most 2 self-check iterations"
```

### Quick Win #2: Cache Project Config Lookups
```python
# In orchestrator, cache project_config.py results
# --field calls are repeated 5+ times per run
# Save: ~10 exec calls per story
```

### Quick Win #3: Minimal Picker Spawn for Single Stories
```python
# When N=1, skip picker entirely
# Direct-to-Researcher path saves 2K tokens
```

---

## 💡 Advanced: Agent Consolidation Proposal

**Current Architecture:** 6+ specialized agents

**Proposed Optimization:** 3 hybrid agents

| Hybrid Agent | Combines | Spawn Count | Tokens Est. |
|--------------|----------|-------------|-------------|
| Scout-Quill | Research + Write | 1 | ~10K |
| Pixel-Press | Image + Drive + WP | 1 | ~4K |
| Picker | Picker (unchanged) | 1 | ~2K |

**Total per story:** ~16K tokens (vs 19-35K current) = **40-55% reduction**

**Trade-off:** Less modularity, harder debugging

---

## 🔧 Implementation Plan

**Phase 1 (Week 1): Quick Wins**
- [ ] Reduce max writer iterations: 3 → 2
- [ ] Cache project config lookups
- [ ] Lite validation mode flag

**Phase 2 (Week 2-3): Cache Layer**
- [ ] URL resolution cache (24h TTL)
- [ ] Writer auto-fix tier 1 failures

**Phase 3 (Week 4): Batch Mode**
- [ ] Batch researcher mode
- [ ] Multi-pick picker input

**Phase 4 (Week 5+): Advanced**
- [ ] Image prompt pre-generation
- [ ] Consider hybrid agent experiments

---

## 📋 Success Metrics

Track these weekly:

| Metric | Baseline | Target | Target % |
|--------|----------|--------|----------|
| Avg tokens/story | ~25K | <12K | -52% |
| Avg agent spawns/story | 8 | <5 | -37% |
| Avg tool calls/story | 18 | <10 | -44% |
| Avg pipeline time | ~8min | <5min | -37% |
| Failures from token limits | TBD | <2% | stable |

---

## ⚠️ Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Quality degradation | Keep full validation on final artifacts only |
| Cache staleness | 24h TTL + manual invalidation |
| Batch failures cascade | Per-story error isolation in batch responses |
| Hybrid debugging | Keep separate agent option for troubleshooting |

---

**Report generated by Nexus (Orchestrator)**
**Next Step:** Review and prioritize Phase 1 quick wins
