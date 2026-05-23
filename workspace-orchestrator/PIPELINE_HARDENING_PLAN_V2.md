# Crypto News Pipeline Hardening Plan — V2

> Status: Developer-facing report only
> 
> This document is a corrected and strengthened version of the prior hardening plan.
> It does **not** make any changes itself. It is a blueprint for implementation.

---

## Executive Summary

The crypto news pipeline is capable of completing successfully, but it is brittle because it depends on:

- freeform agent output instead of strict contracts
- weak validation between steps
- shared temp-file paths
- implicit handoffs between agents
- delivery-oriented command behavior being used for machine capture
- limited post-publish verification

The highest-impact path to stability is:

1. stop relying on noisy interactive output for writer capture
2. add structured result contracts earlier
3. validate every step before proceeding
4. isolate each run’s artifacts
5. persist run state in a manifest
6. verify WordPress rendering after publish

---

# 1. Overall verdict on the existing plan

The existing hardening plan is **good and directionally strong**.

It correctly prioritizes:
- PTY/noise contamination in writer output
- file-based handoffs
- per-run working directories
- manifest-based resume
- validators
- WordPress smoke checks
- placeholder replacement accounting

However, it should be revised before implementation in the following areas:

1. Do **not** rely on undocumented `openclaw agent` flags such as `--no-color` or `--no-spinner` unless the CLI actually supports them.
2. Do **not** treat output cleaning/regex stripping as the primary guarantee of correctness.
3. Move strict JSON contracts earlier in the roadmap.
4. Keep canonical artifact ownership with the orchestrator where possible.
5. Treat symlink-based `/tmp` compatibility as transitional, not final architecture.
6. Strengthen the rendered WordPress smoke test.
7. Tighten validator severity for required template elements.

---

# 2. Important correction: CLI assumptions

## Observed CLI support
Live `openclaw agent --help` shows documented support for options like:
- `--deliver`
- `--json`
- `--thinking`
- `--timeout`
- `--verbose`

## Not confirmed in CLI help
The following options were **not** shown in help output and therefore should be treated as unverified:
- `--no-color`
- `--no-spinner`

## Developer guidance
Replace any implementation language that assumes those flags exist with:

> Use only documented non-interactive or machine-readable output modes supported by `openclaw agent`. Do not rely on undocumented flags unless confirmed in the actual runtime environment.

---

# 3. Root Causes of Pipeline Instability

## 3.1 Writer output contamination
Observed behavior showed writer output files containing:
- spinner text
- “Waiting for agent reply…”
- terminal/control noise
- mixed transport chatter and content

This is a concrete, observed failure and should be treated as the top priority.

## 3.2 Weak contracts between agents
The orchestrator currently has to infer meaning from freeform text outputs.

That creates instability in:
- research handoff
- article generation
- publish link extraction
- WordPress result validation

## 3.3 Shared temp-file reuse
Using fixed paths like:
- `/tmp/crypto-article.md`
- `/tmp/chart.png`
- `/tmp/crypto-feature.jpg`

creates stale-file and cross-run contamination risk.

## 3.4 Resume depends on memory/context instead of state
The pipeline currently relies too much on conversational context when resuming after pauses, approvals, or reconnects.

## 3.5 Post-publish success is under-verified
A returned URL is not enough. The rendered draft should be checked for correctness.

---

# 4. Revised Implementation Priorities

## Phase 1 — Critical Stability
These changes should happen first.

1. Writer capture hardening
2. Per-run artifact isolation
3. Manifest persistence
4. Research validation
5. Writer validation
6. Move partial structured contracts earlier

## Phase 2 — Reliability Hardening
7. Chart validation
8. Feature image validation
9. Publisher structured result
10. WP placeholder accounting
11. Rendered WordPress smoke checks
12. Failure-type-based retries

## Phase 3 — Full contract-driven orchestration
13. Strict result envelopes for all agents
14. Explicit resume from manifest
15. Artifact freshness tracking
16. Full run summary/reporting

---

# 5. Revised Critical Fixes

## Fix 1: Writer capture must avoid noisy delivery-oriented output

### Problem
The pipeline currently uses delivery-style agent execution to produce machine-captured article files. This is brittle because user-facing delivery behavior can contaminate stdout with progress text or terminal noise.

### Recommendation
The developer should redesign writer capture to prefer one of the following, in order:

1. **Documented machine-readable mode** (best)
2. **Documented non-delivery stdout capture mode**
3. **Sanitized fallback cleanup only if necessary**

### Strong recommendation
Noise stripping should be a **fallback safeguard**, not the primary correctness mechanism.

### Implementation guidance
- avoid PTY capture for writer output if content is being written to file
- avoid relying on `--deliver` for article file generation if a cleaner capture path exists
- prefer `--json` or final-only capture if supported by the workflow

### Acceptable fallback
If cleanup is still required, it may strip:
- ANSI escape sequences
- `<thinking>` blocks
- spinner/progress lines

But the system should aim to prevent contamination at source.

---

## Fix 2: Canonical artifact ownership should stay with the orchestrator

### Original plan risk
Having the researcher directly write `/tmp/research.json` is practical, but it blurs ownership.

### Recommended architecture
- agent returns structured result
- orchestrator validates result
- orchestrator writes canonical file artifact

### Why this is better
This keeps responsibilities clean:
- **workers** produce data
- **orchestrator** owns pipeline state and artifacts

### Preferred flow
1. Researcher returns JSON
2. Orchestrator validates it
3. Orchestrator writes `research.json` into the run directory
4. Writer/creator consume that canonical file

---

## Fix 3: Per-run working directory

### Recommendation
Every pipeline run should have an isolated working directory such as:

```bash
RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="/tmp/crypto-run-${RUN_ID}"
mkdir -p "$RUN_DIR"
```

### Canonical artifacts
Example layout:

```text
/tmp/crypto-run-20260518-083000/
  research.json
  article.md
  chart.png
  feature.jpg
  article.docx
  manifest.json
  wp-result.json
```

### Transitional compatibility
If legacy scripts require fixed `/tmp/...` paths, symlinks may be used **temporarily**.

### Important note
Symlink compatibility should be treated as a migration bridge, not final architecture.

---

## Fix 4: Persist pipeline manifest

### Problem
“Continue pipeline” should not depend on fading chat memory.

### Recommendation
Create and update a manifest after every successful step.

### Suggested manifest shape
```json
{
  "run_id": "20260518-083000",
  "current_step": "writer",
  "steps": {
    "research": {"status": "ok", "attempts": 1},
    "writer": {"status": "ok", "attempts": 2},
    "chart": {"status": "pending", "attempts": 0}
  },
  "story": {
    "primary_headline": "...",
    "chart_coin": "bitcoin"
  },
  "artifacts": {
    "research_json": "/tmp/crypto-run-.../research.json",
    "article_md": "/tmp/crypto-run-.../article.md",
    "chart_png": "/tmp/crypto-run-.../chart.png",
    "feature_jpg": "/tmp/crypto-run-.../feature.jpg",
    "docx": "/tmp/crypto-run-.../article.docx"
  },
  "results": {
    "google_doc_url": "",
    "wordpress_draft_url": ""
  }
}
```

### Additional recommendation
Track:
- attempt counts
- validation results
- step timestamps
- artifact mtimes/sizes

---

# 6. Revised Validators

## 6.1 Research validator

### Must validate
- JSON parses successfully
- required fields exist
- source URLs are present
- `chart_coin` exists
- `topic_theme` exists
- `combined_key_facts` is not empty

### Minimum required fields
- `primary_headline`
- `topic_theme`
- `combined_key_facts`
- `source_urls`
- `chart_coin`
- `primary_asset` or equivalent asset field

### Failure behavior
If invalid:
- retry once with strict “valid JSON only” instruction
- if still invalid, stop pipeline

---

## 6.2 Writer validator

This validator should be stricter than the earlier draft plan.

### Hard-fail checks
- no ANSI escape/control sequences
- no `Waiting for agent reply`
- no `<thinking>` blocks
- exactly one H1 title
- required structure present
- word count within allowed bounds
- no duplicated metadata block

### Required template elements
If the article template requires these, they should be **hard failures**, not warnings:
- Sources section
- Word Count block
- expected article structure

### Placeholder checks
Record the exact number of chart placeholders expected downstream.

### Suggested ranges
- target: 1100–1200 words
- acceptable: 1000–1300
- warn: 1301–1500
- fail: <900 or >1500

---

## 6.3 Chart validator

### Must validate
- file exists
- file is PNG
- non-zero size
- dimensions are sane
- requested coin matches result metadata

---

## 6.4 Feature image validator

### Must validate
- file exists
- mime type is expected image format
- non-zero size
- dimensions are sane

---

## 6.5 Publish validator

### Must validate
- docx exists
- docx non-zero size
- publisher returned real file id and URL
- URL format is sane

---

## 6.6 WordPress validator

### Must validate
- draft URL returned
- post id returned
- feature image upload status known
- chart placeholder accounting known
- rendered page smoke check passes

---

# 7. Placeholder Accounting in WP Publisher

This is one of the strongest recommendations and should stay.

## Recommendation
The WP publish stage should report:
- placeholders expected
- placeholders found
- placeholders replaced
- chart uploaded true/false
- feature image uploaded true/false

## Better result contract
```json
{
  "status": "ok",
  "post_id": 91,
  "draft_url": "https://example.com/?p=91",
  "post_status": "draft",
  "feature_image_uploaded": true,
  "chart_uploaded": true,
  "chart_placeholders_expected": 2,
  "chart_placeholders_found": 2,
  "chart_placeholders_replaced": 2
}
```

## Important implementation note
Any sample Python used for placeholder accounting must import all referenced modules (e.g. `os` if used).

---

# 8. Rendered WordPress Smoke Test

This should remain in the plan and be strengthened.

## Minimum checks
After WordPress draft creation, fetch the rendered draft URL and verify:
- no raw `[CHART_PLACEHOLDER]`
- no `/tmp/chart.png`
- no `/tmp/crypto...` paths

## Stronger checks recommended
Also verify:
- title appears in rendered HTML
- feature image/media appears if expected
- at least one uploaded chart/media reference appears if placeholders were expected
- article body length is above a minimum threshold

## Why this matters
A WordPress publish is not “done” because a URL was returned.
It is done when the **rendered draft is correct**.

---

# 9. Failure-Type-Based Retries

The current generic retry pattern should be replaced with targeted retries.

## Recommended retry table

| Failure Type | Retry Strategy |
|---|---|
| Research output invalid JSON | Retry once with strict JSON-only instruction |
| Writer output noisy | Rerun writer using the cleanest documented capture path |
| Writer too long | Run dedicated compression repair once |
| Writer missing required structure | Run structure repair once |
| Chart missing/corrupt | Rerun chart step once |
| Feature image missing/corrupt | Rerun creator once |
| Publish returned incomplete result | Retry extraction once |
| WP placeholder mismatch | Fail loudly; do not silently continue |

## Explicit retry ceilings
Recommended:
- research: 1 repair retry
- writer: 1 clean rerun + 1 structure/compression repair
- chart: 1 rerun
- creator: 1 rerun
- publish: 1 extraction retry
- wp: 0 silent retries on placeholder mismatch

This prevents infinite soft-failure loops.

---

# 10. Move Structured Contracts Earlier

## Problem with previous plan
Strict JSON contracts were placed too late.

## Recommendation
Move them earlier:

### Phase 1 / early Phase 2
At minimum, implement structured outputs for:
- researcher
- publisher
- wp-publisher

### Phase 2
Then extend to:
- writer
- chart-generator
- creator

### Phase 3
Standardize full result envelopes everywhere.

## Why
Contract-driven output is not a late optimization; it is part of the core stability fix.

---

# 11. Suggested Result Contracts

## Researcher
```json
{
  "status": "ok",
  "story_id": "string",
  "primary_headline": "string",
  "topic_theme": "string",
  "primary_keyword": "string",
  "primary_asset": "string",
  "chart_coin": "string",
  "confidence": 0.0,
  "source_urls": ["string"],
  "combined_key_facts": ["string"]
}
```

## Writer
```json
{
  "status": "ok",
  "title": "string",
  "word_count": 1184,
  "chart_placeholders": 2,
  "article_path": "/tmp/.../article.md"
}
```

## Chart generator
```json
{
  "status": "ok",
  "chart_coin": "bitcoin",
  "days": 30,
  "output_path": "/tmp/.../chart.png",
  "mime": "image/png"
}
```

## Creator
```json
{
  "status": "ok",
  "output_path": "/tmp/.../feature.jpg",
  "mime": "image/jpeg"
}
```

## Publisher
```json
{
  "status": "ok",
  "file_id": "abc123",
  "webViewLink": "https://docs.google.com/..."
}
```

## WP Publisher
```json
{
  "status": "ok",
  "post_id": 91,
  "draft_url": "https://example.com/?p=91",
  "post_status": "draft",
  "feature_image_uploaded": true,
  "chart_uploaded": true,
  "chart_placeholders_expected": 2,
  "chart_placeholders_found": 2,
  "chart_placeholders_replaced": 2
}
```

---

# 12. Final Recommended Priority Order

## Highest-priority fixes
1. clean writer capture strategy
2. per-run working directory
3. manifest persistence
4. strict research validator
5. strict writer validator
6. partial structured outputs early

## Next-highest
7. chart/image artifact validators
8. publisher structured result
9. wp placeholder accounting
10. rendered WordPress smoke test
11. failure-type-based retries

## After that
12. full contract-driven orchestration
13. explicit resume from manifest
14. artifact freshness tracking
15. run-level reporting

---

# 13. Concise Developer Guidance

Use the current hardening plan as the base roadmap, but revise it with these rules:

1. Do not rely on undocumented `openclaw agent` flags.
2. Prefer documented machine-readable or non-interactive capture for writer output.
3. Treat cleanup regexes as fallback only.
4. Keep canonical artifact ownership in the orchestrator where possible.
5. Use per-run directories.
6. Persist manifest state after every step.
7. Move structured contracts earlier.
8. Treat symlink compatibility as temporary.
9. Make required article template elements hard-fail validations.
10. Strengthen WordPress smoke tests beyond placeholder absence.

---

# 14. Final Verdict

The original plan is **good**, but this V2 version is safer and more implementation-ready.

## Short verdict
- Diagnosis quality: high
- Priority ordering: strong
- Architecture direction: correct
- Required revisions: yes

## Final recommendation
Implement this as a staged hardening effort, beginning with:
- writer capture cleanup at source
- per-run state/artifact isolation
- validators
- manifest
- early structured contracts

These changes will remove the majority of observed instability.
