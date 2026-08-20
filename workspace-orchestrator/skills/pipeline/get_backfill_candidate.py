#!/usr/bin/env python3
"""get_backfill_candidate.py — Pick the next-best fresh pool story to replace a
failed one, so a batch still reaches its target count.

Pure Python, NO LLM. Returns ONE candidate from the project's headline_pool
(status fresh|shown), newest first, excluding URLs already in the current batch
and any already drafted/published for the project. Marks the chosen URL
'selected' and writes a selection file the orchestrator feeds to
build_picker_input.py --selection-file (classify-only).

Usage:
  python3 get_backfill_candidate.py --project coinnetwork \
      --exclude-urls "https://a,https://b" \
      --output /tmp/<slug>-backfill.json

Prints:
  BACKFILL_CANDIDATE: url=<url> headline=<...> selection_file=<path>
  BACKFILL_NONE: project=<slug>   (pool exhausted)
Always exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402
import project_config as pc  # noqa: E402


def _consumed_urls(project: str, db_path: str) -> set[str]:
    db.init_db(db_path)
    urls: set[str] = set()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT DISTINCT primary_url FROM picked_stories
                WHERE primary_url IS NOT NULL AND primary_url != ''
                  AND status IN ('drafted', 'published')
                  AND project = ?
                """,
                (project,),
            ).fetchall()
            urls = {r["primary_url"] for r in rows if r["primary_url"]}
    except sqlite3.Error:
        pass
    return urls


def main() -> int:
    p = argparse.ArgumentParser(description="Pick a backfill replacement story")
    p.add_argument("--project", default=None)
    p.add_argument("--exclude-urls", default="", help="Comma-separated URLs already in the batch")
    p.add_argument("--exclude-file", default=None, help="JSON file with urls[] to exclude")
    p.add_argument("--output", default=None, help="Selection file to write (default /tmp/<slug>-backfill.json)")
    args = p.parse_args()

    try:
        cfg = pc.load_project_config(slug=args.project)
        project = cfg.slug
    except (FileNotFoundError, ValueError) as e:
        print(f"BACKFILL_NONE: config_error {e}")
        return 0

    exclude: set[str] = set()
    if args.exclude_urls:
        exclude.update(u.strip() for u in args.exclude_urls.split(",") if u.strip())
    if args.exclude_file and os.path.exists(args.exclude_file):
        try:
            with open(args.exclude_file, encoding="utf-8") as f:
                data = json.load(f)
            exclude.update(str(u) for u in (data.get("urls") or []))
        except (OSError, json.JSONDecodeError):
            pass
    exclude.update(_consumed_urls(project, db.DEFAULT_DB_PATH))

    candidates = db.available_for_backfill(project, exclude_urls=list(exclude), limit=1)
    if not candidates:
        print(f"BACKFILL_NONE: project={project}")
        return 0

    chosen = candidates[0]
    db.mark_pool(project, [chosen.url], "selected")
    out_path = args.output or f"/tmp/{project}-backfill.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"project": project, "urls": [chosen.url]}, f, ensure_ascii=False)
    except OSError as e:
        print(f"BACKFILL_NONE: write_failed {e}")
        return 0

    print(
        f"BACKFILL_CANDIDATE: url={chosen.url} headline={chosen.headline[:80]} "
        f"selection_file={out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
