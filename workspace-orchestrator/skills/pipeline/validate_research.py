#!/usr/bin/env python3
"""
validate_research.py — Parse researcher raw output, validate it,
and atomically write research/validated.json + update manifest story.

Usage:
    python3 validate_research.py --manifest /path/to/manifest.json
    python3 validate_research.py --manifest /path/to/manifest.json \
        --raw-path /path/to/raw_2.json \
        --validated-path /path/to/validated_2.json

Reads:   manifest.artifacts.research_raw  (default; override with --raw-path)
Writes:  manifest.artifacts.research_validated (default; override with --validated-path)
         manifest.json  (updates story.story_id + story.headline + current_step)

When `--raw-path` and/or `--validated-path` are supplied (Picker batch loop),
the manifest's artifact paths are NOT overwritten — only the per-iteration
validated file is produced. The manifest's `current_step` and `story` block
are still updated for in-flight visibility.

Exit 0 + prints RESEARCH_VALID: <headline>
Exit 1 + prints RESEARCH_INVALID: <reason>
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import argparse

# ── shared research checks (single source of truth with Scout's self-check) ──
_RESEARCH_CHECK_DIR = os.path.expanduser(
    "~/.openclaw/workspace-researcher/skills/research-check"
)
if _RESEARCH_CHECK_DIR not in sys.path:
    sys.path.insert(0, _RESEARCH_CHECK_DIR)

import check_research as cr  # noqa: E402
import project_config as pc  # noqa: E402
import wp_category_resolve as wcr  # noqa: E402

REQUIRED_FIELDS = cr.RESEARCH_REQUIRED_FIELDS


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def load_manifest(manifest_path: str) -> dict:
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--raw-path",
        default=None,
        help="Override path to researcher raw JSON (default: manifest.artifacts.research_raw).",
    )
    parser.add_argument(
        "--validated-path",
        default=None,
        help="Override path to write validated JSON (default: manifest.artifacts.research_validated).",
    )
    parser.add_argument(
        "--picks",
        default=None,
        help="Path to picks.json. When given with --pick-index, the pick's "
        "category + wp_category_slugs + wp_category_ids are injected "
        "authoritatively into validated.json (overrides researcher copy).",
    )
    parser.add_argument(
        "--pick-index",
        type=int,
        default=None,
        help="1-based pick index to look up in --picks.",
    )
    args = parser.parse_args()

    manifest_path = os.path.realpath(args.manifest)
    if not os.path.exists(manifest_path):
        print(f"RESEARCH_INVALID: manifest not found: {manifest_path}")
        return 1

    try:
        manifest = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"RESEARCH_INVALID: cannot parse manifest: {e}")
        return 1

    artifacts = manifest.get("artifacts", {})
    raw_path = args.raw_path or artifacts.get("research_raw", "")
    validated_path = args.validated_path or artifacts.get("research_validated", "")
    run_dir = manifest.get("run_dir", "")

    if not raw_path or not os.path.exists(raw_path):
        print(f"RESEARCH_INVALID: research_raw not found: {raw_path}")
        return 1

    # Read raw file — may have extra text around JSON (PTY noise, etc.)
    try:
        with open(raw_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as e:
        print(f"RESEARCH_INVALID: cannot read raw file: {e}")
        return 1

    # Extract JSON block (shared lenient extractor)
    data, _ = cr.extract_json_block(text)

    # A clean error JSON is a deliberate "skip this story" signal, not valid
    # research — report it so the orchestrator drops the pick cleanly.
    if isinstance(data, dict) and data.get("status") == "error":
        print(f"RESEARCH_INVALID: {data.get('reason') or 'researcher error'}")
        return 1

    # Run the shared deep-research checklist — the exact rules Scout
    # self-checks on, so the gate and self-check can never diverge.
    results = cr.run_deep_research_checks(data, text)
    failures = [(name, detail) for name, ok, detail in results if not ok]
    if failures:
        name, detail = failures[0]
        print(f"RESEARCH_INVALID: {name}: {detail}")
        return 1

    data.pop("tweet_quotes", None)

    # Authoritatively inject WP category info from the pick (if provided).
    # picks.json is the source of truth (validated + id-resolved by
    # validate_picks.py), so this overrides whatever the researcher copied.
    wp_category_slugs = data.get("wp_category_slugs")
    wp_category_ids = data.get("wp_category_ids")
    if args.picks and args.pick_index is not None and os.path.exists(args.picks):
        try:
            with open(args.picks, encoding="utf-8") as f:
                picks_doc = json.load(f)
            match = next(
                (
                    p
                    for p in (picks_doc.get("picks") or [])
                    if int(p.get("pick_index", -1)) == int(args.pick_index)
                ),
                None,
            )
            if match:
                if isinstance(match.get("wp_category_slugs"), list):
                    wp_category_slugs = match["wp_category_slugs"]
                if isinstance(match.get("wp_category_ids"), list):
                    wp_category_ids = match["wp_category_ids"]
                if match.get("category"):
                    data["category"] = match["category"]
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"[WARN] Could not read pick categories from {args.picks}: {e}", file=sys.stderr)

    if isinstance(wp_category_slugs, list):
        data["wp_category_slugs"] = wp_category_slugs
    if isinstance(wp_category_ids, list):
        data["wp_category_ids"] = wp_category_ids

    # Resolve slug -> numeric ids from project config when ids are missing.
    project_slug = manifest.get("project") or "coinography"
    wp_categories: list[dict] = []
    try:
        cfg = pc.load_project_config(slug=project_slug)
        raw_cats = cfg.get_path("wordpress.categories", []) or []
        wp_categories = [c for c in raw_cats if isinstance(c, dict)]
    except (FileNotFoundError, ValueError):
        pass

    slugs = data.get("wp_category_slugs")
    ids = data.get("wp_category_ids")
    if wp_categories and isinstance(slugs, list) and slugs and not ids:
        resolved_slugs, resolved_ids = wcr.resolve_slugs_to_ids(slugs, wp_categories)
        if resolved_ids:
            data["wp_category_slugs"] = resolved_slugs
            data["wp_category_ids"] = resolved_ids

    # Coin-aware guard: force lead-coin category when distinctive tokens match.
    if wp_categories:
        new_slugs, new_ids, note = wcr.apply_coin_category_guard(
            data,
            wp_categories,
            wp_category_slugs=data.get("wp_category_slugs"),
            wp_category_ids=data.get("wp_category_ids"),
        )
        if note:
            data["wp_category_slugs"] = new_slugs
            data["wp_category_ids"] = new_ids
            print(f"[INFO] {note}", file=sys.stderr)

    # Atomically write validated.json
    if not validated_path:
        # Fall back to run_dir if artifact key missing (shouldn't happen)
        validated_path = os.path.join(run_dir, "research", "validated.json")

    try:
        atomic_write_json(validated_path, data)
    except OSError as e:
        print(f"RESEARCH_INVALID: cannot write validated.json: {e}")
        return 1

    # Update manifest story + current_step
    try:
        manifest["story"] = {
            "story_id": data.get("story_id", ""),
            "headline": data.get("primary_headline", ""),
            "chart_coin": data.get("chart_coin", "bitcoin"),
            "category": data.get("category") or manifest.get("story", {}).get("category", ""),
            "wp_category_slugs": data.get("wp_category_slugs")
            or manifest.get("story", {}).get("wp_category_slugs", []),
            "wp_category_ids": data.get("wp_category_ids")
            or manifest.get("story", {}).get("wp_category_ids", []),
        }
        manifest["current_step"] = "research_validated"
        atomic_write_json(manifest_path, manifest)
    except (OSError, Exception) as e:
        # Non-fatal — validated.json is already written
        print(f"[WARN] Could not update manifest story: {e}", file=sys.stderr)

    headline = data["primary_headline"][:70]
    chart_coin = data.get("chart_coin", "bitcoin")
    print(f"RESEARCH_VALID: {headline}")
    print(f"CHART_COIN: {chart_coin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
