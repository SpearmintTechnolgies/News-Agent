#!/usr/bin/env python3
"""
validate_picks.py — Parse the picker's picks.json output, validate it against
the schema and the original picker_input.json, and insert validated picks into
editorial.db's picked_stories table.

Usage:
    python3 validate_picks.py \
        --picks /path/to/picks.json \
        --picker-input /path/to/picker_input.json \
        --pick-run-id <run_id-pick> \
        [--pipeline-run-id <run_id>] \
        [--db-path /path/to/editorial.db]

Exit 0 + prints PICKS_VALID: <count> picks for <pick_run_id> ids=<ids>
Exit 1 + prints PICKS_INVALID: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db  # noqa: E402

VALID_CATEGORIES = {
    "regulation",
    "etf_institutional",
    "hack_exploit",
    "l1_l2_protocol",
    "exchange",
    "stablecoin",
    "adoption_partnership",
    "market_movement",
}

REQUIRED_PICK_FIELDS = [
    "pick_index",
    "candidate_index",
    "category",
    "headline",
    "url",
]


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def _read_json_loose(path: str) -> dict | None:
    """Read a file that may have prose around the JSON object."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    if not text.strip():
        return None
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--picks", required=True)
    parser.add_argument("--picker-input", required=True)
    parser.add_argument("--pick-run-id", required=True)
    parser.add_argument("--pipeline-run-id", default=None)
    parser.add_argument("--db-path", default=editorial_db.DEFAULT_DB_PATH)
    args = parser.parse_args()

    picks_path = os.path.realpath(args.picks)
    input_path = os.path.realpath(args.picker_input)

    pdata = _read_json_loose(picks_path)
    if pdata is None:
        print(f"PICKS_INVALID: cannot read or parse picks file: {picks_path}")
        return 1

    if pdata.get("status") == "error":
        reason = pdata.get("reason") or "unspecified"
        detail = pdata.get("detail") or ""
        print(f"PICKS_INVALID: picker reported status=error reason={reason} detail={detail}")
        return 1
    if pdata.get("status") != "ok":
        print(f"PICKS_INVALID: status must be 'ok', got {pdata.get('status')!r}")
        return 1

    try:
        with open(input_path, encoding="utf-8") as f:
            idata = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"PICKS_INVALID: cannot read picker_input.json: {e}")
        return 1

    target_count = int(idata.get("target_count") or 0)
    cand_indices = {
        int(c.get("candidate_index"))
        for c in (idata.get("candidates") or [])
        if isinstance(c, dict) and c.get("candidate_index") is not None
    }
    if not cand_indices:
        print("PICKS_INVALID: picker_input has no candidate_index values")
        return 1

    picks = pdata.get("picks")
    if not isinstance(picks, list) or not picks:
        print("PICKS_INVALID: picks must be a non-empty list")
        return 1

    if len(picks) > target_count:
        print(f"PICKS_INVALID: too many picks ({len(picks)} > target {target_count})")
        return 1

    seen_pick_indices: set[int] = set()
    seen_candidate_indices: set[int] = set()
    cleaned_picks: list[dict] = []
    for i, p in enumerate(picks, 1):
        if not isinstance(p, dict):
            print(f"PICKS_INVALID: pick #{i} is not an object")
            return 1
        missing = [k for k in REQUIRED_PICK_FIELDS if not p.get(k) and p.get(k) != 0]
        if missing:
            print(f"PICKS_INVALID: pick #{i} missing required fields: {missing}")
            return 1
        try:
            pick_index = int(p["pick_index"])
            candidate_index = int(p["candidate_index"])
        except (TypeError, ValueError):
            print(f"PICKS_INVALID: pick #{i} pick_index/candidate_index must be ints")
            return 1
        if pick_index != i:
            print(
                f"PICKS_INVALID: pick #{i} has non-contiguous pick_index={pick_index} "
                f"(must equal position {i})"
            )
            return 1
        if pick_index in seen_pick_indices:
            print(f"PICKS_INVALID: duplicate pick_index={pick_index}")
            return 1
        if candidate_index in seen_candidate_indices:
            print(f"PICKS_INVALID: duplicate candidate_index={candidate_index}")
            return 1
        if candidate_index not in cand_indices:
            print(
                f"PICKS_INVALID: pick #{i} candidate_index={candidate_index} not in picker_input"
            )
            return 1

        category = str(p.get("category") or "").strip()
        if category not in VALID_CATEGORIES:
            print(f"PICKS_INVALID: pick #{i} unknown category: {category!r}")
            return 1

        seen_pick_indices.add(pick_index)
        seen_candidate_indices.add(candidate_index)

        cleaned = {
            "pick_index": pick_index,
            "candidate_index": candidate_index,
            "category": category,
            "category_score": p.get("category_score"),
            "alt_categories": p.get("alt_categories") or [],
            "selection_score": p.get("selection_score"),
            "slot_score": p.get("slot_score"),
            "story_id": p.get("story_id"),
            "headline": str(p.get("headline") or "").strip(),
            "url": str(p.get("url") or "").strip(),
            "primary_url": str(p.get("url") or "").strip(),
            "pub_date": p.get("pub_date"),
            "source": p.get("source"),
            "source_name": p.get("source"),
            "summary": p.get("summary"),
            "corroborating_sources": p.get("corroborating_sources") or [],
            "reason": p.get("reason"),
        }
        cleaned_picks.append(cleaned)

    rewritten = dict(pdata)
    rewritten["picked_count"] = len(cleaned_picks)
    rewritten["picks"] = cleaned_picks
    try:
        atomic_write_json(picks_path, rewritten)
    except OSError as e:
        print(f"PICKS_INVALID: cannot rewrite picks.json: {e}")
        return 1

    try:
        ids = editorial_db.insert_picked_stories(
            cleaned_picks,
            pick_run_id=args.pick_run_id,
            pipeline_run_id=args.pipeline_run_id,
            db_path=args.db_path,
        )
    except (sqlite3.Error, ValueError) as e:
        print(f"PICKS_INVALID: db insert failed: {e}")
        return 1

    print(
        f"PICKS_VALID: {len(cleaned_picks)} picks for {args.pick_run_id} "
        f"ids={ids} categories={[p['category'] for p in cleaned_picks]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
