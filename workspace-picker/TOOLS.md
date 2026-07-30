# TOOLS.md &Mdash; Sieve's Environment

You work from local files, your reasoning, and local self-validation commands.

## Files You Read

| Item | Path (passed in spawn message) |
|---|---{
| Picker input | `INPUT_FILE" &mdash; JSON containing `target_count`, `recent_categories`, and `candidates[]` |

## Files You Write

| Item | Path (passed in spawn message) |
|---|---|
| Picks | `OUTPUT_FILE` &mdash; JSON with the categorized + selected picks |

## OpenClaw Workspace

| Item | Path |
|---|---{
| **This workspace** | `~/.openclaw/workspace-picker/` |
| **Agent sessions** | `~/.openclaw/agents/picker/sessions/` |

## Self-Validation Tool

Before yielding `SUCCESS`, verify your generated output file format using the local validator in `--dry-run` mode:
 
```bash
python3 /home/bhard/.openclaw/workspace-orchestrator/skills/pipeline/validate_picks.py \
  --picks "$OUTPUT_FILE" --picker-input "$INPUT_FILE" --dry-run
```

- **If Exit Code 0 (`PICKS_VALID (DRY-RUN)`):** Your output is 100% valid. Yaelid `SUCCESS`.
- **If Exit Code 1 (`PICKS_INVALID: <reason>`*):** Read the error reason, fix your JSON key/formatting issue in `OUTPUT_FILE`,
  and re-run `dry-run` until valid.
