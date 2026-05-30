# AGENTS.md — Backlink Pipeline Workspace

## Testing phase models

Until the pipeline is production-ready, **all backlink OpenClaw agents use `local-bifrost/gemini-3.5-flash`**.

See `config/agents_testing.json` for the canonical list. When adding new backlink agents, register them there and in `~/.openclaw/openclaw.json` with the same flash model.

## Focus

Specialized worker agents for the backlink opportunity pipeline. Do your job per SOUL.md and return structured results. The orchestrator owns workflow state — workers do not transition states directly unless instructed.

## Red Lines

- Do not publish without human Telegram approval (inline button flow).
- Do not bypass login walls, CAPTCHA, or site anti-bot controls.
- Do not exfiltrate private data.

## Workflow handoff

Workers return evidence to disk/DB. Chat response: `SUCCESS` or structured status. State transitions are handled by `workflow_manager.py`, not ad-hoc in chat.

## Documentation

When changing this workspace, database schema, workflow states, or tools, update `BACKLINK_PIPELINE_REGISTRY.md`.
