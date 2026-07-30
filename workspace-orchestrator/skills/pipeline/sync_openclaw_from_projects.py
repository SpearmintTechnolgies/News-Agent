#!/usr/bin/env python3
"""
sync_openclaw_from_projects.py — Derive openclaw.json Telegram groups + bindings
from projects/*.json (single source of truth).

Usage:
    python3 sync_openclaw_from_projects.py --dry-run
    python3 sync_openclaw_from_projects.py --apply
    python3 sync_openclaw_from_projects.py --apply --skip-restart
    python3 sync_openclaw_from_projects.py --dry-run --slug coinography
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import project_config as pc  # noqa: E402
from openclaw_telegram_sync import (  # noqa: E402
    OPENCLAW_JSON,
    atomic_write_json,
    backup_openclaw,
    check_gateway_health,
    load_openclaw,
    restart_gateway,
    run_ensure_scheduler,
    sync_projects_into_config,
    validate_json_roundtrip,
)


def collect_project_bindings(*, slug_filter: str | None = None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for slug in pc.list_available_projects():
        if slug_filter and slug != slug_filter:
            continue
        try:
            cfg = pc.load_project_config(slug=slug)
        except (FileNotFoundError, ValueError) as e:
            print(f"SYNC_WARN: skip {slug}: {e}", file=sys.stderr)
            continue
        group_id = str(cfg.get_path("telegram.group_id") or "").strip()
        if not group_id:
            print(f"SYNC_WARN: skip {slug}: no telegram.group_id", file=sys.stderr)
            continue
        name = str(cfg.get("name") or slug)
        out.append((slug, group_id, name))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Sync only this project slug")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print plan only (default)")
    mode.add_argument("--apply", action="store_true", help="Write openclaw.json")
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="With --apply: write config but skip gateway restart",
    )
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    projects = collect_project_bindings(slug_filter=args.slug)
    if not projects:
        print("SYNC_ERROR: no projects with telegram.group_id found")
        return 1

    try:
        cfg = load_openclaw()
    except (OSError, ValueError) as e:
        print(f"SYNC_ERROR: cannot read/parse {OPENCLAW_JSON}: {e}")
        return 1

    new_cfg, changes = sync_projects_into_config(cfg, projects)

    if not validate_json_roundtrip(new_cfg):
        print("SYNC_ERROR: patched config failed JSON validation")
        return 1

    print(f"SYNC_PLAN: {len(projects)} project(s) from projects/*.json")
    for slug, gid, _ in projects:
        print(f"  - {slug} -> group {gid}")
    if changes:
        for c in changes:
            print(f"  - {c}")
    else:
        print("  - (no changes needed)")

    if not apply:
        print("\nSYNC_DRY_RUN: no files written. Re-run with --apply to write.")
        return 0

    if new_cfg == cfg:
        print("SYNC_OK: openclaw.json already in sync")
        return 0

    try:
        backup_path = backup_openclaw()
        print(f"SYNC_BACKUP: {backup_path}")
    except OSError as e:
        print(f"SYNC_ERROR: backup failed: {e}")
        return 1

    try:
        atomic_write_json(OPENCLAW_JSON, new_cfg)
        on_disk = load_openclaw()
        if on_disk != new_cfg:
            raise AssertionError("post-write content mismatch")
    except (OSError, ValueError, AssertionError) as e:
        print(f"SYNC_ERROR: write failed ({e}). Restore from backup if needed.")
        return 1

    print(f"SYNC_WRITTEN: {OPENCLAW_JSON}")

    if args.skip_restart:
        print("SYNC_SKIP_RESTART: config written; gateway will pick up on next restart.")
        return 0

    ok, out = restart_gateway()
    if out.strip():
        print(out.strip())
    if not ok:
        print("SYNC_WARN: gateway restart did not report success — restart manually.")
        return 1

    healthy, health_out = check_gateway_health()
    if not healthy:
        print(f"SYNC_WARN: gateway health check failed: {health_out}")
        return 1
    print("SYNC_GATEWAY_HEALTHY: gateway restarted")

    sched_ok, sched_out = run_ensure_scheduler()
    if sched_out.strip():
        print(sched_out.strip())
    if not sched_ok:
        print("SYNC_WARN: ensure_scheduler.sh did not report success")

    print("SYNC_OK: telegram bindings synced from projects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
