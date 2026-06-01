#!/usr/bin/env python3
"""
check_recent_topic_duplicates.py — Compare validated research to recent topic registry.

Usage:
    python3 check_recent_topic_duplicates.py \
        --current /path/to/validated.json \
        --registry ~/.openclaw/workspace-orchestrator/state/recent_topics.json \
        --window-hours 24

Exit 0 + prints TOPIC_FRESH: <reason>
Exit 1 + prints TOPIC_DUPLICATE: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


STOPWORDS = {
    "the", "and", "for", "its", "are", "was", "has", "have", "with", "that",
    "this", "from", "not", "but", "can", "will", "all", "new", "law", "bill",
    "act", "says", "said", "into", "over", "after", "about", "their", "they",
}

KEYWORD_FAMILIES = [
    "strategic bitcoin reserve",
    "bitcoin reserve",
    "arma",
    "ethereum foundation",
    "xrp etf",
    "solana etf",
    "exchange hack",
    "sec lawsuit",
]

HEADLINE_OVERLAP_MIN = 3
THEME_OVERLAP_MIN = 3


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def token_set(text: str) -> set[str]:
    return {
        w
        for w in normalize_text(text).split()
        if len(w) >= 3 and w not in STOPWORDS
    }


def overlap_count(a: str, b: str) -> int:
    return len(token_set(a) & token_set(b))


def normalize_asset(asset: str) -> str:
    return normalize_text(asset)


def keyword_families(text: str) -> set[str]:
    norm = normalize_text(text)
    return {fam for fam in KEYWORD_FAMILIES if fam in norm}


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_timestamp(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def prune_registry(entries: list[dict], window_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    kept: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts = parse_timestamp(entry.get("timestamp", ""))
        if ts is None:
            kept.append(entry)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(entry)
    return kept


def topic_fields(data: dict) -> dict[str, str]:
    return {
        "story_id": (data.get("story_id") or "").strip(),
        "primary_headline": (data.get("primary_headline") or "").strip(),
        "topic_theme": (data.get("topic_theme") or "").strip(),
        "primary_asset": (data.get("primary_asset") or "").strip(),
    }


def is_duplicate(current: dict, prior: dict) -> str | None:
    cur = topic_fields(current)
    old = topic_fields(prior)

    if cur["story_id"] and old["story_id"] and cur["story_id"] == old["story_id"]:
        return f"same story_id: {cur['story_id']}"

    h_overlap = overlap_count(cur["primary_headline"], old["primary_headline"])
    if h_overlap >= HEADLINE_OVERLAP_MIN:
        return (
            f"headline overlap ({h_overlap} tokens) with run {prior.get('run_id', '?')}: "
            f"{old['primary_headline'][:60]}"
        )

    cur_asset = normalize_asset(cur["primary_asset"])
    old_asset = normalize_asset(old["primary_asset"])
    t_overlap = overlap_count(cur["topic_theme"], old["topic_theme"])
    if cur_asset and old_asset and cur_asset == old_asset and t_overlap >= THEME_OVERLAP_MIN:
        return (
            f"same asset ({cur['primary_asset']}) and theme overlap ({t_overlap} tokens) "
            f"with run {prior.get('run_id', '?')}"
        )

    cur_text = " ".join(
        [cur["primary_headline"], cur["topic_theme"], cur["primary_asset"]]
    )
    old_text = " ".join(
        [old["primary_headline"], old["topic_theme"], old["primary_asset"]]
    )
    cur_fams = keyword_families(cur_text)
    old_fams = keyword_families(old_text)
    shared_fams = cur_fams & old_fams
    if shared_fams:
        fam = sorted(shared_fams)[0]
        return (
            f"shared keyword family '{fam}' with run {prior.get('run_id', '?')}"
        )

    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--window-hours", type=int, default=24)
    args = parser.parse_args()

    current_path = os.path.realpath(args.current)
    registry_path = os.path.realpath(args.registry)

    if not os.path.exists(current_path):
        print(f"TOPIC_DUPLICATE: current file not found: {current_path}")
        return 1

    try:
        current = load_json(current_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"TOPIC_DUPLICATE: cannot parse current research: {e}")
        return 1

    if not isinstance(current, dict):
        print("TOPIC_DUPLICATE: current research must be a JSON object")
        return 1

    entries: list[dict] = []
    if os.path.exists(registry_path):
        try:
            raw = load_json(registry_path)
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError):
            entries = []

    recent = prune_registry(entries, args.window_hours)

    for prior in recent:
        reason = is_duplicate(current, prior)
        if reason:
            print(f"TOPIC_DUPLICATE: {reason}")
            return 1

    headline = topic_fields(current)["primary_headline"][:60] or "ok"
    print(f"TOPIC_FRESH: no match in last {args.window_hours}h ({len(recent)} recent entries) — {headline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
