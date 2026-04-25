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

## Temporary Artifacts
*These are generated during the pipeline run and overwritten on the next run.*
- **Raw Article Text:** `/tmp/crypto-article.md`
- **Generated Image:** `/tmp/crypto-feature.jpg`
- **Merged Article (No Image):** `/tmp/crypto-with-image.md`
- **Final Document:** `/tmp/crypto-article.docx`

## Documentation
- **Knowledge Base:** `/home/bhard/.openclaw/PIPELINE_DOCS/`
