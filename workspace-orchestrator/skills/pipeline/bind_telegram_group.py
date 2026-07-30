#!/usr/bin/env python3
"""
bind_telegram_group.py — Atomically patch openclaw.json to wire a Telegram
group to a project (groups ACL + systemPrompt + orchestrator binding).
Then (optionally) restart the gateway and scheduler.

Prefer sync_openclaw_from_projects.py for full sync from projects/*.json.
This script remains for single-project manual binds.

Usage:
    python3 bind_telegram_group.py --slug coinnetwork --group-id -100123 --name Coinnetwork --dry-run
    python3 bind_telegram_group.py --slug coinnetwork --group-id -100123 --name Coinnetwork --apply
"""
from __future__ import annotations

import argparse
import sys

HERE = __import__("os").path.dirname(__import__("os").path.realpath(__file__))
sys.path.insert(0, HERE)

from openclaw_telegram_sync import (  # noqa: E402
    OPENCLAW_JSON,
    atomic_write_json,
    backup_openclaw,
    build_patch,
    check_gateway_health,
    load_openclaw,
    restart_gateway,
    run_ensure_scheduler,
    validate_json_roundtrip,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--name", required=True, help="Human display name for the systemPrompt")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the patch, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="Write openclaw.json, backup first, then restart gateway")
    parser.add_argument("--skip-restart", action="store_true", help="With --apply: write config but skip gateway restart")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    try:
        cfg = load_openclaw()
    except (OSError, ValueError) as e:
        print(f"BIND_ERROR: cannot read/parse {OPENCLAW_JSON}: {e}")
        return 1

    new_cfg, changes = build_patch(cfg, slug=args.slug, group_id=args.group_id, name=args.name)

    if not validate_json_roundtrip(new_cfg):
        print("BIND_ERROR: patched config failed JSON validation")
        return 1

    print(f"BIND_PLAN: {args.slug} -> group {args.group_id}")
    for c in changes:
        print(f"  - {c}")

    if not apply:
        print("\nBIND_DRY_RUN: no files were written. Re-run with --apply to write + restart the gateway.")
        return 0

    try:
        backup_path = backup_openclaw()
        print(f"BIND_BACKUP: {backup_path}")
    except OSError as e:
        print(f"BIND_ERROR: backup failed, aborting before write: {e}")
        return 1

    try:
        atomic_write_json(OPENCLAW_JSON, new_cfg)
        on_disk = load_openclaw()
        if on_disk != new_cfg:
            raise AssertionError("post-write content mismatch")
    except (OSError, ValueError, AssertionError) as e:
        print(f"BIND_ERROR: post-write verification failed ({e}). Restore from backup if needed.")
        return 1

    print(f"BIND_WRITTEN: {OPENCLAW_JSON} updated (backup at {backup_path})")

    if args.skip_restart:
        print("BIND_SKIP_RESTART: config written; restart the gateway manually to apply.")
        return 0

    ok, out = restart_gateway()
    print(out.strip())
    if not ok:
        print(
            "BIND_WARN: gateway restart did not report success. Config is written and backed up. "
            "Restart manually and verify with `openclaw gateway status`."
        )
        return 1

    healthy, health_out = check_gateway_health()
    if not healthy:
        print(
            f"BIND_WARN: gateway restarted but health check failed: {health_out}. "
            "Investigate before relying on the new binding."
        )
        return 1
    print("BIND_GATEWAY_HEALTHY: gateway restarted and responded to health check")

    sched_ok, sched_out = run_ensure_scheduler()
    print(sched_out.strip())
    if not sched_ok:
        print("BIND_WARN: ensure_scheduler.sh did not report success — scanner/feed cards may not run until it is (re)started.")

    print(f"BIND_OK: {args.slug} bound to group {args.group_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
