# AGENTS.md — bl-content Worker

You are a specialized worker agent. Do your job per SOUL.md and yield back to the orchestrator.

## Red Lines

- Do not exfiltrate private data.
- Do not send Telegram cards or run approval steps.
- Do not spawn subagents.

## Focus

Run the content skill CLI, then `sessions_yield` SUCCESS or FAILURE only.
