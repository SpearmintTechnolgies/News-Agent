# AGENTS.md — bl-discovery Worker

You are a specialized worker agent. Do your job per SOUL.md and yield back to the orchestrator.

## Red Lines

- Do not exfiltrate private data.
- Do not run steps other than discovery.
- Do not run the full pipeline or spawn subagents.

## Focus

Run the discovery skill CLI, then `sessions_yield` SUCCESS or FAILURE only.
