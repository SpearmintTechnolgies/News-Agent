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


REQUIRED_FIELDS = [
    "story_id",
    "primary_headline",
    "topic_theme",
    "combined_key_facts",
    "source_urls",
    "chart_coin",
]


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

    # Extract JSON block
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        print("RESEARCH_INVALID: no JSON object found in research_raw")
        return 1

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        print(f"RESEARCH_INVALID: JSON parse error: {e}")
        return 1

    # Field validation
    missing = [k for k in REQUIRED_FIELDS if not data.get(k)]
    if missing:
        print(f"RESEARCH_INVALID: missing fields: {missing}")
        return 1

    if len(data.get("combined_key_facts", [])) < 2:
        print("RESEARCH_INVALID: combined_key_facts must have at least 2 entries")
        return 1

    if not isinstance(data.get("source_urls", []), list) or not data["source_urls"]:
        print("RESEARCH_INVALID: source_urls must be a non-empty list")
        return 1

    data.pop("tweet_quotes", None)

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
