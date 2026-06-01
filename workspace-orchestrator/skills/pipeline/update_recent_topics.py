#!/usr/bin/env python3
"""
update_recent_topics.py — Append or update an entry in the recent topics registry.

Usage:
    python3 update_recent_topics.py \
        --current /path/to/validated.json \
        --registry ~/.openclaw/workspace-orchestrator/state/recent_topics.json \
        --run-id 20260522-120000 \
        --status researched|drafted|published \
        [--published-url URL]

Exit 0 + prints TOPIC_REGISTRY_UPDATED: <run_id> <status>
Exit 1 + prints TOPIC_REGISTRY_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any


VALID_STATUSES = {"researched", "drafted", "published"}


def atomic_write_json(path: str, data: Any) -> None:
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(dir_, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def entry_from_research(data: dict, run_id: str, status: str, published_url: str | None) -> dict:
    entry: dict[str, Any] = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "story_id": (data.get("story_id") or "").strip(),
        "primary_headline": (data.get("primary_headline") or "").strip(),
        "topic_theme": (data.get("topic_theme") or "").strip(),
        "primary_asset": (data.get("primary_asset") or "").strip(),
        "status": status,
    }
    if published_url:
        entry["published_url"] = published_url.strip()
    return entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--status", required=True, choices=sorted(VALID_STATUSES))
    parser.add_argument("--published-url", default="")
    args = parser.parse_args()

    current_path = os.path.realpath(args.current)
    registry_path = os.path.realpath(args.registry)
    run_id = args.run_id.strip()
    status = args.status.strip().lower()

    if not run_id:
        print("TOPIC_REGISTRY_ERROR: --run-id is required")
        return 1

    if not os.path.exists(current_path):
        print(f"TOPIC_REGISTRY_ERROR: current file not found: {current_path}")
        return 1

    try:
        current = load_json(current_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"TOPIC_REGISTRY_ERROR: cannot parse current research: {e}")
        return 1

    if not isinstance(current, dict):
        print("TOPIC_REGISTRY_ERROR: current research must be a JSON object")
        return 1

    published_url = (args.published_url or "").strip() or None

    entries: list[dict] = []
    if os.path.exists(registry_path):
        try:
            raw = load_json(registry_path)
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError):
            entries = []

    new_entry = entry_from_research(current, run_id, status, published_url)

    updated = False
    for i, existing in enumerate(entries):
        if existing.get("run_id") == run_id:
            merged = {**existing, **new_entry}
            if status == "published" and published_url:
                merged["published_url"] = published_url
            elif existing.get("published_url") and not published_url:
                merged["published_url"] = existing["published_url"]
            entries[i] = merged
            updated = True
            break

    if not updated:
        entries.append(new_entry)

    try:
        atomic_write_json(registry_path, entries)
    except OSError as e:
        print(f"TOPIC_REGISTRY_ERROR: cannot write registry: {e}")
        return 1

    action = "updated" if updated else "added"
    print(f"TOPIC_REGISTRY_UPDATED: {run_id} {status} ({action})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
