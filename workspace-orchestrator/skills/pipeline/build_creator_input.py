#!/usr/bin/env python3
"""
build_creator_input.py — Extract image fields for Pixel.

Feature images must follow the SOURCE STORY PHOTO (subject/setting), not a
generic 3D coin from the category slug.

Usage:
    python3 build_creator_input.py --research <validated.json> --output <creator_input.json>

Prints HEADLINE / CATEGORY / SCENE_HINT / SOURCE_IMAGE / IMAGE_CONTEXT
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fetch_source_image as fsi  # noqa: E402


def _story_context(research: dict) -> str:
    """One short line of story context for Pixel (not a visual invention)."""
    bits: list[str] = []
    for key in ("topic_theme", "primary_headline", "primary_keyword"):
        val = str(research.get(key) or "").strip()
        if val and val not in bits:
            bits.append(val)
    for fact in research.get("combined_key_facts") or []:
        text = " ".join(str(fact).split())
        if len(text) < 40 or text.lower().startswith("news"):
            continue
        if any(n in text.lower() for n in ("spacex", "nvidia", "social security", "cisco")):
            continue
        bits.append(text[:160])
        break
    return " ".join(bits)[:280]


def story_scene_hint(headline: str, has_source: bool, context: str = "") -> str:
    h = (headline or "this crypto news story").strip()
    extra = f" Story: {context}." if context else ""
    if has_source:
        return (
            f"Use the attached source photo as reference only for: {h}.{extra} "
            "Original 16:9 editorial: same mood and subject, new composition. "
            "Do not copy the reference layout or wordmark. No readable text."
        )
    return (
        f"Editorial news image of this story (not a generic 3D coin): {h}.{extra} "
        "16:9 cinematic crypto editorial, no text."
    )


def main():
    parser = argparse.ArgumentParser(description="Build creator input from research")
    parser.add_argument("--research", required=True, help="Path to validated.json")
    parser.add_argument("--output", required=True, help="Output path for creator_input.json")
    parser.add_argument("--project", default="coinnetwork", help="Project slug")
    args = parser.parse_args()

    try:
        with open(args.research, "r", encoding="utf-8") as f:
            research = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to read research file: {e}", file=sys.stderr)
        sys.exit(1)

    headline = research.get("primary_headline", "")
    category = research.get("category", "")
    primary_keyword = research.get("primary_keyword", "")

    if not headline or not category:
        print(
            f"ERROR: Missing required fields (headline={bool(headline)}, category={bool(category)})",
            file=sys.stderr,
        )
        sys.exit(1)

    out_abs = os.path.abspath(args.output)
    run_dir = os.path.dirname(os.path.dirname(out_abs))
    source_dest = os.path.join(run_dir, "media", "source.jpg")
    source_path, source_url = fsi.resolve_and_save(research, source_dest)
    if not source_path:
        fsi.atomic_placeholder_cleanup(source_dest)

    context = _story_context(research)
    scene_hint = story_scene_hint(headline, bool(source_path), context)
    creator_input = {
        "headline": headline,
        "category": category,
        "primary_keyword": primary_keyword,
        "project": args.project,
        "scene_hint": scene_hint,
        "image_context": context,
        "source_image_url": source_url or research.get("source_image_url") or "",
        "source_image": source_path or "",
    }

    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    with open(out_abs, "w", encoding="utf-8") as f:
        json.dump(creator_input, f, indent=2)

    print(f"CREATOR_INPUT_BUILT: {len(json.dumps(creator_input))} chars -> {out_abs}")
    print(f"HEADLINE: {headline}")
    print(f"CATEGORY: {category}")
    print(f"SCENE_HINT: {scene_hint}")
    print(f"IMAGE_CONTEXT: {context or scene_hint}")
    if source_path:
        print(f"SOURCE_IMAGE: {source_path}")
        print(f"SOURCE_IMAGE_URL: {source_url}")
    else:
        print("SOURCE_IMAGE:")
        print("SOURCE_IMAGE_MISS: no usable hero image on source story")


if __name__ == "__main__":
    main()
