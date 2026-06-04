#!/usr/bin/env python3
"""
build_picker_input.py — Build picker_input.json from validated headlines.json,
filtering out URLs that the picker has already consumed in past runs and
attaching the recent-categories context the Picker needs for diversity.

Usage:
    python3 build_picker_input.py \
        --headlines /path/to/headlines.json \
        --output /path/to/picker_input.json \
        --target-count 3 \
        [--recent-window-hours 24] \
        [--db-path /path/to/editorial.db]

Reads:  headlines.json (cleaned by validate_headlines.py)
        editorial.db   (recent_published_categories + already-consumed pick URLs)
Writes: picker_input.json

Exit 0 + prints PICKER_INPUT_BUILT: <count> candidates / target=<N> / recent=<...>
Exit 1 + prints PICKER_INPUT_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db  # noqa: E402  (local import after sys.path insert)


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def _consumed_pick_urls(db_path: str) -> set[str]:
    """All primary URLs of picks that ever reached drafted/published.

    Used to filter out candidates that have been consumed in past pipeline
    runs (permanent dedup at the picker level — independent of the 7-day
    article_history.db).
    """
    editorial_db.init_db(db_path)
    urls: set[str] = set()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT primary_url FROM picked_stories
            WHERE primary_url IS NOT NULL AND primary_url != ''
              AND status IN ('drafted', 'published')
            """
        ).fetchall()
        for r in rows:
            urls.add(r["primary_url"])
    return urls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headlines", required=True, help="Path to validated headlines.json")
    parser.add_argument("--output", required=True, help="Path to write picker_input.json")
    parser.add_argument("--target-count", type=int, required=True, help="N stories to pick")
    parser.add_argument("--recent-window-hours", type=int, default=24)
    parser.add_argument("--db-path", default=editorial_db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    if args.target_count < 1:
        print(f"PICKER_INPUT_ERROR: target_count must be >= 1, got {args.target_count}")
        return 1

    headlines_path = os.path.realpath(args.headlines)
    if not os.path.exists(headlines_path):
        print(f"PICKER_INPUT_ERROR: headlines not found: {headlines_path}")
        return 1

    try:
        with open(headlines_path, encoding="utf-8") as f:
            hdata = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"PICKER_INPUT_ERROR: cannot parse headlines.json: {e}")
        return 1

    candidates_in = hdata.get("candidates") or []
    if not isinstance(candidates_in, list) or not candidates_in:
        print("PICKER_INPUT_ERROR: headlines.json has no candidates")
        return 1

    try:
        consumed = _consumed_pick_urls(args.db_path)
    except sqlite3.Error as e:
        print(f"PICKER_INPUT_ERROR: db read failed: {e}")
        return 1

    try:
        recent_categories = editorial_db.recent_published_categories(
            hours=int(args.recent_window_hours), db_path=args.db_path
        )
    except sqlite3.Error as e:
        print(f"PICKER_INPUT_ERROR: db read failed: {e}")
        return 1

    candidates_out: list[dict] = []
    skipped_consumed = 0
    for c in candidates_in:
        if not isinstance(c, dict):
            continue
        url = str(c.get("url") or "").strip()
        if not url:
            continue
        if url in consumed:
            skipped_consumed += 1
            continue
        candidates_out.append({
            "candidate_index": len(candidates_out) + 1,
            "headline": str(c.get("headline") or "").strip(),
            "url": url,
            "pub_date": c.get("pub_date"),
            "source": str(c.get("source") or "Unknown"),
            "summary": str(c.get("summary") or ""),
            "corroborating_sources": c.get("corroborating_sources") or [],
        })

    if not candidates_out:
        print(
            f"PICKER_INPUT_ERROR: all {len(candidates_in)} candidates filtered out "
            f"(consumed={skipped_consumed})"
        )
        return 1

    out = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "target_count": int(args.target_count),
        "recent_window_hours": int(args.recent_window_hours),
        "recent_categories": recent_categories,
        "skipped_consumed_urls": skipped_consumed,
        "candidates": candidates_out,
    }

    try:
        atomic_write_json(os.path.realpath(args.output), out)
    except OSError as e:
        print(f"PICKER_INPUT_ERROR: cannot write output: {e}")
        return 1

    print(
        f"PICKER_INPUT_BUILT: {len(candidates_out)} candidates / target={args.target_count} / "
        f"recent={recent_categories or 'none'} / skipped_consumed={skipped_consumed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
