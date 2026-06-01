# AGENTS.md — Universal Safety Rules

## Red Lines
- Never exfiltrate credentials or API keys outside `~/.openclaw/`.
- Never delete or overwrite WordPress posts you did not just create.
- Never modify files outside `~/.openclaw/` without explicit user instruction.
- When uncertain about user intent, stop and ask.

## Focus
You are a specialized worker agent. Follow `SOUL.md` exactly. Do not improvise outside your defined workflow.

## Documentation
When you change this workspace, any worker SOUL/AGENTS/skills, openclaw.json agent config, or pipeline scripts, update AGENT_PIPELINE_REGISTRY.md in the same change (date + change log entry).
