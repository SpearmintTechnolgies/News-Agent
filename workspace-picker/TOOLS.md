# TOOLS.md — Sieve's Environment

You have **no external tools**. You work entirely from local files and your own reasoning.

## Files You Read

| Item | Path (passed in spawn message) |
|---|---|
| Picker input | `INPUT_FILE` — JSON containing `target_count`, `recent_categories`, and `candidates[]` |

## Files You Write

| Item | Path (passed in spawn message) |
|---|---|
| Picks | `OUTPUT_FILE` — JSON with the categorized + selected picks |

## OpenClaw Workspace

| Item | Path |
|---|---|
| **This workspace** | `~/.openclaw/workspace-picker/` |
| **Agent sessions** | `~/.openclaw/agents/picker/sessions/` |

## Skills

This workspace ships no skill scripts — your only skill is reasoning and writing valid JSON. The orchestrator validates your output via `validate_picks.py`; if your JSON is malformed or violates the contract, you'll be re-spawned with the validator error.
