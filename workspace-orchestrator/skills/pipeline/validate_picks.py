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
import project_config as pc  # noqa: E402

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
    parser.add_argument("--pick-run-id", default=None)
    parser.add_argument("--pipeline-run-id", default=None)
    parser.add_argument("--db-path", default=editorial_db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--project",
        default=None,
        help="Project slug; default: resolved from PROJECT_SLUG / manifest / coinnetwork",
    )
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Keep every pick; do NOT enforce batch primary-category uniqueness "
        "(human-selected / backfill flow where the picker only labels categories).",
    )
    parser.add_argument(
        "--append-to",
        default=None,
        help="Canonical picks.json to APPEND these picks to (backfill). pick_index "
        "continues after the existing max; new picks are merged into that file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate picks.json against picker_input.json without DB insertion or file mutation.",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.pick_run_id:
        print("PICKS_INVALID: --pick-run-id is required unless --dry-run is specified")
        return 1

    try:
        cfg = pc.load_project_config(slug=args.project)
    except (FileNotFoundError, ValueError) as e:
        print(f"PICKS_INVALID: {e}")
        return 1
    project_slug = cfg.slug

    # Resolve WP category vocabulary. slug_to_id covers the FULL live category
    # list (used to resolve any slug → numeric id). curated_set is the narrowed
    # primary allow-list the Picker was given; empty means "all allowed".
    wp_categories = cfg.get_path("wordpress.categories", []) or []
    slug_to_id = {
        str(c.get("slug")): int(c.get("id"))
        for c in wp_categories
        if isinstance(c, dict) and c.get("slug") and c.get("id") is not None
    }
    if not slug_to_id:
        print(
            f"PICKS_INVALID: project '{project_slug}' has no wordpress.categories. "
            f"Run sync_wp_categories.py --slug {project_slug} first."
        )
        return 1
    curated_set = set(cfg.get_path("wordpress.picker_category_slugs", []) or [])
    try:
        fallback_id = int(cfg.get_path("wordpress.fallback_category_id", 17))
    except (TypeError, ValueError):
        fallback_id = 17
    id_to_slug = {v: k for k, v in slug_to_id.items()}
    fallback_slug = id_to_slug.get(fallback_id) or next(iter(slug_to_id.keys()))

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

    idata = _read_json_loose(input_path)
    if not isinstance(idata, dict):
        print(f"PICKS_INVALID: cannot read picker_input.json: {input_path}")
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

    # classify-only (flag OR picker_input marker): keep every pick, skip the
    # hard batch primary-category uniqueness. Backfill/human-selected flow.
    classify_only = bool(args.classify_only) or bool(idata.get("classify_only"))
    diversity_relaxed = bool(pdata.get("diversity_relaxed")) or classify_only

    # Backfill: continue pick_index after the existing canonical picks.json max.
    index_offset = 0
    existing_picks: list[dict] = []
    if args.append_to:
        existing = _read_json_loose(os.path.realpath(args.append_to)) or {}
        existing_picks = existing.get("picks") or []
        if isinstance(existing_picks, list) and existing_picks:
            try:
                index_offset = max(int(ep.get("pick_index") or 0) for ep in existing_picks)
            except (TypeError, ValueError):
                index_offset = len(existing_picks)

    seen_pick_indices: set[int] = set()
    seen_candidate_indices: set[int] = set()
    seen_primary_slugs: set[str] = set()
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

        # --- WP category resolution -----------------------------------------
        # Build the slug list: prefer wp_category_slugs; fall back to [category].
        raw_slugs = p.get("wp_category_slugs")
        if not isinstance(raw_slugs, list) or not raw_slugs:
            raw_slugs = [p.get("category")]
        slug_list: list[str] = []
        for s in raw_slugs:
            s = str(s or "").strip()
            if s and s not in slug_list:
                slug_list.append(s)
        if not slug_list:
            print(f"PICKS_INVALID: pick #{i} has no category slugs")
            return 1

        # Picker output can include unknown/unapproved primary slugs (e.g. `arbitrum`)
        # in `wp_category_slugs[0]`. Validate_picks used to hard-fail in that case,
        # which caused infinite retry loops on FEED_DRAIN.
        #
        # Instead, pick the first known slug, preferring curated_set when present.
        primary = None
        curated_primary_candidates = [
            s for s in slug_list if s in slug_to_id and (not curated_set or s in curated_set)
        ]
        if curated_primary_candidates:
            primary = curated_primary_candidates[0]
        else:
            any_known = [s for s in slug_list if s in slug_to_id]
            primary = any_known[0] if any_known else fallback_slug

        # Hard batch-uniqueness on the PRIMARY slug, unless the picker relaxed.
        if primary in seen_primary_slugs and not diversity_relaxed:
            print(
                f"PICKS_INVALID: pick #{i} repeats primary category {primary!r} in the "
                f"same batch (set diversity_relaxed=true only when the pool forces it)"
            )
            return 1

        # Resolve known slugs → ids (preserve order, dedup). Unknown secondary
        # slugs are dropped with a warning rather than failing the batch.
        resolved_slugs: list[str] = []
        resolved_ids: list[int] = []
        ordered_slugs = [primary] + [s for s in slug_list if s != primary]
        for s in ordered_slugs:
            if s in slug_to_id:
                if s not in resolved_slugs:
                    resolved_slugs.append(s)
                    resolved_ids.append(slug_to_id[s])
            else:
                print(f"[WARN] pick #{i} dropping unknown secondary slug {s!r}", file=sys.stderr)
        if not resolved_ids:
            resolved_ids = [fallback_id]
            resolved_slugs = [fallback_slug]
            primary = fallback_slug

        seen_pick_indices.add(pick_index)
        seen_candidate_indices.add(candidate_index)
        seen_primary_slugs.add(primary)

        cleaned = {
            "pick_index": index_offset + pick_index,
            "candidate_index": candidate_index,
            "category": primary,
            "wp_category_slugs": resolved_slugs,
            "wp_category_ids": resolved_ids,
            "category_score": p.get("category_score"),
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

    if args.dry_run:
        print(
            f"PICKS_VALID (DRY-RUN): project={project_slug} / {len(cleaned_picks)} picks valid "
            f"primary={[p['category'] for p in cleaned_picks]} "
            f"wp_category_ids={[p['wp_category_ids'] for p in cleaned_picks]}"
            + (" diversity_relaxed=true" if diversity_relaxed else "")
            + (" classify_only=true" if classify_only else "")
        )
        return 0

    # Insert the NEW picks into the DB regardless of mode.
    try:
        ids = editorial_db.insert_picked_stories(
            cleaned_picks,
            pick_run_id=args.pick_run_id,
            pipeline_run_id=args.pipeline_run_id,
            project=project_slug,
            db_path=args.db_path,
        )
    except (sqlite3.Error, ValueError) as e:
        print(f"PICKS_INVALID: db insert failed: {e}")
        return 1

    if args.append_to:
        # Backfill: merge new picks into the canonical picks.json so the
        # researcher (which reads INPUT_FILE by PICK_INDEX) sees them.
        append_path = os.path.realpath(args.append_to)
        merged = _read_json_loose(append_path) or {"status": "ok", "picks": []}
        merged_picks = list(merged.get("picks") or [])
        merged_picks.extend(cleaned_picks)
        merged["picks"] = merged_picks
        merged["picked_count"] = len(merged_picks)
        try:
            atomic_write_json(append_path, merged)
        except OSError as e:
            print(f"PICKS_INVALID: cannot append to picks.json: {e}")
            return 1
        new_indices = [p["pick_index"] for p in cleaned_picks]
        print(
            f"PICKS_APPENDED: project={project_slug} / {len(cleaned_picks)} picks "
            f"into {append_path} ids={ids} pick_index={new_indices} "
            f"primary={[p['category'] for p in cleaned_picks]}"
        )
        return 0

    rewritten = dict(pdata)
    rewritten["picked_count"] = len(cleaned_picks)
    rewritten["picks"] = cleaned_picks
    try:
        atomic_write_json(picks_path, rewritten)
    except OSError as e:
        print(f"PICKS_INVALID: cannot rewrite picks.json: {e}")
        return 1

    print(
        f"PICKS_VALID: project={project_slug} / {len(cleaned_picks)} picks for "
        f"{args.pick_run_id} ids={ids} "
        f"primary={[p['category'] for p in cleaned_picks]} "
        f"wp_category_ids={[p['wp_category_ids'] for p in cleaned_picks]}"
        + (" diversity_relaxed=true" if diversity_relaxed else "")
        + (" classify_only=true" if classify_only else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
