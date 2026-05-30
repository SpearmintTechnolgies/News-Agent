# USER.md — Backlink Pipeline

- **Notes:** Use `sessions_spawn` + `sessions_yield` for all pipeline worker steps (discover, score, audit, content). The orchestrator must **never** run `workflow_driver_cli.py step` or `run` — those commands are for worker agents only. Direct exec of worker steps bypasses the agent architecture and breaks the pipeline.
