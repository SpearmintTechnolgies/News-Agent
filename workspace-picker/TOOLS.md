# TOOLS.md — Sieve (Windows)

You only **read** `INPUT_FILE` and **write** `OUTPUT_FILE`. No web. No Telegram.

## Windows

- Paths look like `C:/tmp/coinnetwork-run-…/picker/picker_input.json`
- `/tmp/...` and `/home/bhard/...` do **not** exist. Never use them.
- Never run `python3`, `validate_picks.py`, or `exec`. Nexus validates after you write the file.

## First two tool calls (mandatory)

1. `read` the exact `INPUT_FILE` path from the spawn message.
2. `write` JSON to the exact `OUTPUT_FILE` path.

If `read` fails, `write` `{"status":"error","reason":"input_unreadable"}` to `OUTPUT_FILE` and stop.

## After write

Reply with only: `SUCCESS`
