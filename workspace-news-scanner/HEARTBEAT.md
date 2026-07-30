# Scanner watchdog — keep pool_scheduler.py alive

Run the launcher script, then reply HEARTBEAT_OK:

```bash
/home/bhard/.openclaw/workspace-orchestrator/skills/pipeline/ensure_scheduler.sh
```

If `pool_scheduler.py` is already running, the script exits 0 with no side effects.
