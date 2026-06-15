#!/usr/bin/env python3
"""update_headline_pool.py — 24x7 scanner that fills the per-project headline pool.

Pure Python, NO LLM. Designed to run as an OpenClaw cron `--command` job
(zero tokens). It reuses the deterministic researcher scanner
(`scan_headlines.run_scan`) to fetch + dedupe RSS headlines, then upserts the
survivors into `editorial.db`'s `headline_pool` table — STRICTLY per project,
so the two project lists can never mix.

Usage:
  python3 update_headline_pool.py --project memecoinist
  python3 update_headline_pool.py --all          # scan every project in turn
  python3 update_headline_pool.py --all --target-count 25 --prune-hours 168

Always exits 0 (cron-friendly). Prints a summary to stderr and `NO_REPLY` to
stdout so any accidental delivery is suppressed.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

_PIPELINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PIPELINE)
_HEADLINE_SCAN = os.path.expanduser(
    "~/.openclaw/workspace-researcher/skills/headline-scan"
)
sys.path.insert(0, _HEADLINE_SCAN)

import editorial_db as db  # noqa: E402
import project_config as pc  # noqa: E402

try:
    import scan_headlines  # noqa: E402
except Exception as e:  # pragma: no cover - import guard
    print(f"POOL_ERROR: cannot import scan_headlines: {e}", file=sys.stderr)
    print("NO_REPLY")
    sys.exit(0)


def update_one(project_slug: str, *, target_count: int, prune_hours: int) -> str:
    """Scan one project and upsert into its pool. Returns a status line."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=f"pool-scan-{project_slug}-", suffix=".json")
        os.close(fd)
        code, result = scan_headlines.run_scan(
            project_slug=project_slug,
            output_file=tmp,
            target_count=max(1, target_count),
        )
        if code != 0 or result.get("status") != "ok":
            reason = result.get("reason", "scan_failed")
            return f"POOL_SKIP: project={project_slug} reason={reason}"
        candidates = result.get("candidates", []) or []
        added = db.upsert_pool_candidates(project_slug, candidates)
        pruned = db.prune_pool(project_slug, older_than_hours=prune_hours)
        fresh = len(db.fresh_pool(project_slug, limit=10_000))
        return (
            f"POOL_UPDATED: project={project_slug} scanned={len(candidates)} "
            f"added={added} pruned={pruned} total_fresh={fresh}"
        )
    except Exception as e:  # never let one project break the cron run
        return f"POOL_ERROR: project={project_slug} detail={e}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main() -> int:
    p = argparse.ArgumentParser(description="Fill the per-project headline pool")
    p.add_argument("--project", help="Single project slug")
    p.add_argument("--all", action="store_true", help="Scan every available project in turn")
    p.add_argument(
        "--target-count",
        type=int,
        default=int(os.environ.get("POOL_TARGET_COUNT", "25")),
        help="Max candidates to pull per project per scan (default 25)",
    )
    p.add_argument(
        "--prune-hours",
        type=int,
        default=int(os.environ.get("POOL_PRUNE_HOURS", "168")),
        help="Delete pool rows older than this many hours (default 168 = 7d)",
    )
    args = p.parse_args()

    if args.all:
        projects = pc.list_available_projects()
    elif args.project:
        projects = [args.project]
    else:
        # Fall back to the resolved single project (env/manifest/default).
        projects = [pc.resolve_project_slug()]

    if not projects:
        print("POOL_SKIP: no projects found", file=sys.stderr)
        print("NO_REPLY")
        return 0

    for slug in projects:
        line = update_one(slug, target_count=args.target_count, prune_hours=args.prune_hours)
        print(line, file=sys.stderr)

    # cron --command: suppress delivery regardless of announce config.
    print("NO_REPLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
