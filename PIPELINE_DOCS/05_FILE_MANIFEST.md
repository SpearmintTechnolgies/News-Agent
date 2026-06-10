# File Manifest

This is the absolute mapping of every file related to the Crypto News Pipeline. Future agents should use these absolute paths to read or modify the system logic.

## Core Configuration Files
- **OpenClaw Config:** `/home/bhard/.openclaw/openclaw.json`
- **Security Permissions:** `/home/bhard/.openclaw/exec-approvals.json`

## Agent Logic (SOUL Files)
- **Orchestrator SOUL:** `/home/bhard/.openclaw/workspace-orchestrator/SOUL.md`
- **Researcher SOUL:** `/home/bhard/.openclaw/workspace-researcher/SOUL.md`
- **Writer SOUL:** `/home/bhard/.openclaw/workspace-writer/SOUL.md`
- **Creator SOUL:** `/home/bhard/.openclaw/workspace-creator/SOUL.md`
- **Publisher SOUL:** `/home/bhard/.openclaw/workspace-publisher/SOUL.md`

## Agent Memory (State)
*These are the files the Orchestrator deletes before spawning to prevent hallucination.*
- `/home/bhard/.openclaw/agents/orchestrator/sessions/sessions.json`
- `/home/bhard/.openclaw/agents/researcher/sessions/sessions.json`
- `/home/bhard/.openclaw/agents/writer/sessions/sessions.json`
- `/home/bhard/.openclaw/agents/creator/sessions/sessions.json`
- `/home/bhard/.openclaw/agents/publisher/sessions/sessions.json`

## Temporary Artifacts (Run-Bundle Model)

*As of the run-bundle refactor, all per-run artifacts live under a single isolated run directory.*
*Legacy `/tmp/...` paths are symlinks into the active run-bundle — not real standalone files.*

### Active run location
- **Active run pointer:** `/tmp/crypto-active-run` (contains RUN_DIR path)
- **Run env file:** `/tmp/crypto-run-env.sh` (source to get `$RUN_ID`, `$RUN_DIR`, `$PIPELINE_MANIFEST`)

### Run-bundle structure: `/tmp/crypto-run-<YYYYMMDD-HHMMSS>/`

| Run-bundle path | Legacy `/tmp` symlink | Producer |
|----------------|----------------------|----------|
| `manifest.json` | `/tmp/pipeline-manifest.json` | Step 0 |
| `.run_started` | — | Step 0 (epoch stamp) |
| `research/raw.json` | `/tmp/researcher-raw.txt` | Scout |
| `research/validated.json` | `/tmp/research.json` | validate_research.py |
| `article/raw.md` | `/tmp/crypto-article-raw.md` | Quill |
| `article/final.md` | `/tmp/crypto-article.md` | sync_article_from_raw.py |
| `article/with-image.md` | `/tmp/crypto-with-image.md` | Step 4 |
| `article/article.docx` | `/tmp/crypto-article.docx` | pandoc |
| `media/feature.jpg` | `/tmp/crypto-feature.jpg` | Pixel |
| `media/chart.png` | `/tmp/chart.png` | chart-generator |
| `publish/wordpress.json` | `/tmp/wp-result.json` | wp-publisher |

### Pipeline scripts
| Script | Location |
|--------|----------|
| `init_run.sh` | `workspace-orchestrator/skills/pipeline/init_run.sh` |
| `manifest_paths.py` | `workspace-orchestrator/skills/pipeline/manifest_paths.py` |
| `validate_research.py` | `workspace-orchestrator/skills/pipeline/validate_research.py` |
| `sync_article_from_raw.py` | `workspace-orchestrator/skills/pipeline/sync_article_from_raw.py` |
| `verify_artifacts.py` | `workspace-orchestrator/skills/pipeline/verify_artifacts.py` |
| `update_manifest_step.sh` | `workspace-orchestrator/skills/pipeline/update_manifest_step.sh` |
| `cleanup_run_artifacts.sh` | `workspace-orchestrator/skills/pipeline/cleanup_run_artifacts.sh` |

## Documentation
- **Knowledge Base:** `/home/bhard/.openclaw/PIPELINE_DOCS/`
