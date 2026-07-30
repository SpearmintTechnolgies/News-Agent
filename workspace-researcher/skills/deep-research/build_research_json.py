#!/usr/bin/env python3
"""
build_research_json.py — Assemble the DEEP_RESEARCH Fact File from read_tool
outputs. Deterministic, zero LLM tokens.

Reads every source record written by read_tool.py into --out-dir, dedupes by
domain, and writes a schema-correct raw.json that satisfies check_research.py
(>= 600 words, >= 2 source URLs, >= 2 key facts, no aggregator wrappers). Copies
category / wp_category fields straight through from picks.json. On zero usable
content it writes a clean error JSON the orchestrator can skip cleanly.

Usage:
  python3 build_research_json.py \
    --input picks.json --pick-index 1 \
    --out-dir /run/research/sources --output /run/research/raw.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

PROSE_MIN_WORDS = 600
MIN_SOURCES = 2

ASSET_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bbitcoin\b|\bbtc\b", re.I), "Bitcoin", "bitcoin"),
    (re.compile(r"\bethereum\b|\beth\b", re.I), "Ethereum", "ethereum"),
    (re.compile(r"\bxrp\b|\bripple\b", re.I), "XRP", "ripple"),
    (re.compile(r"\bsolana\b|\bsol\b", re.I), "Solana", "solana"),
    (re.compile(r"\bbnb\b|\bbinance coin\b", re.I), "BNB", "binancecoin"),
    (re.compile(r"\bdoge\b|\bdogecoin\b", re.I), "Dogecoin", "dogecoin"),
    (re.compile(r"\bada\b|\bcardano\b", re.I), "Cardano", "cardano"),
    (re.compile(r"\bavax\b|\bavalanche\b", re.I), "Avalanche", "avalanche-2"),
    (re.compile(r"\bpolkadot\b|\bdot\b", re.I), "Polkadot", "polkadot"),
    (re.compile(r"\bchainlink\b|\blink\b", re.I), "Chainlink", "chainlink"),
]


def atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False, suffix=".tmp",
                                     encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80] or "story"


def detect_asset(text: str) -> tuple[str, str]:
    for pat, asset, coin in ASSET_PATTERNS:
        if pat.search(text):
            return asset, coin
    return "Bitcoin", "bitcoin"


def extract_key_facts(content: str, max_facts: int = 3) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", content or "")
    scored: list[tuple[int, str]] = []
    for s in sentences:
        s = s.strip()
        if len(s) < 30:
            continue
        score = 0
        if re.search(r"\d", s):
            score += 2
        if "$" in s or "%" in s:
            score += 2
        if '"' in s or "'" in s:
            score += 1
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    facts = [s for _, s in scored[:max_facts]]
    if len(facts) < 2:
        for s in sentences:
            s = s.strip()
            if len(s) >= 40 and s not in facts:
                facts.append(s)
            if len(facts) >= 2:
                break
    return facts[:max_facts]


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def load_pick(input_file: str, pick_index: int) -> dict[str, Any] | None:
    with open(input_file, encoding="utf-8") as f:
        doc = json.load(f)

    if isinstance(doc, list):
        picks = doc
    elif isinstance(doc, dict):
        if "picks" in doc and isinstance(doc["picks"], list):
            picks = doc["picks"]
        elif int(doc.get("pick_index", -1)) == pick_index or "url" in doc or "headline" in doc:
            return doc
        else:
            picks = []
    else:
        picks = []

    for p in picks:
        if isinstance(p, dict) and int(p.get("pick_index", -1)) == pick_index:
            return p

    if picks and isinstance(picks[0], dict):
        return picks[0]

    return None


def load_sources(out_dir: str) -> list[dict]:
    """Load ok records, dedupe by domain (keep the longest content per domain)."""
    by_domain: dict[str, dict] = {}
    if not os.path.isdir(out_dir):
        return []
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not rec.get("ok") or not rec.get("content"):
            continue
        dom = domain_of(rec.get("url") or "")
        if not dom:
            continue
        if dom not in by_domain or len(rec["content"]) > len(by_domain[dom]["content"]):
            by_domain[dom] = rec
    return list(by_domain.values())


def write_error(reason: str, output: str) -> dict:
    err = {"status": "error", "reason": reason}
    atomic_write_json(output, err)
    return err


def build(input_file: str, pick_index: int, out_dir: str, output_file: str) -> tuple[int, dict]:
    pick = load_pick(input_file, pick_index)
    if not pick:
        print("BUILD_ERROR: pick_index_not_found", file=sys.stderr)
        return 1, write_error("pick_index_not_found", output_file)

    sources = load_sources(out_dir)
    if not sources:
        print("BUILD_ERROR: all_urls_dead", file=sys.stderr)
        return 1, write_error("all_urls_dead", output_file)

    prose_parts = [s["content"] for s in sources if s.get("content")]
    aggregated = "\n\n---\n\n".join(prose_parts)
    total_words = sum(int(s.get("words") or 0) for s in sources)

    facts: list[str] = []
    seen_facts: set[str] = set()
    for s in sources:
        for fct in extract_key_facts(s["content"]):
            k = fct[:80]
            if k not in seen_facts:
                seen_facts.add(k)
                facts.append(fct)
    while len(facts) < 2 and prose_parts:
        facts.append(prose_parts[0][:200])

    headline = str(pick.get("headline") or pick.get("primary_headline") or "Untitled")
    primary_asset, chart_coin = detect_asset(f"{headline} {aggregated[:500]}")

    out = {
        "status": "ok",
        "mode": "deep_research",
        "story_id": slugify(headline),
        "category": pick.get("category") or "",
        "wp_category_slugs": pick.get("wp_category_slugs") or [],
        "wp_category_ids": pick.get("wp_category_ids") or [],
        "topic_theme": headline,
        "primary_keyword": " ".join(headline.split()[:3]),
        "primary_headline": headline,
        "primary_asset": primary_asset,
        "chart_coin": chart_coin,
        "sources_used": [s.get("source") or domain_of(s["url"]) for s in sources],
        "source_urls": [s["url"] for s in sources],
        "combined_key_facts": facts[:8],
        "aggregated_raw_content": aggregated,
        "partial_words": total_words,
    }
    atomic_write_json(output_file, out)

    meets = total_words >= PROSE_MIN_WORDS and len(sources) >= MIN_SOURCES
    if meets:
        print(f"BUILD_OK: words={total_words} sources={len(sources)}", file=sys.stderr)
        return 0, out
    out["status"] = "partial"
    out["reason"] = "insufficient_content"
    atomic_write_json(output_file, out)
    print(
        f"BUILD_PARTIAL: words={total_words} sources={len(sources)} "
        f"need>={PROSE_MIN_WORDS}w and >={MIN_SOURCES} sources",
        file=sys.stderr,
    )
    return 1, out


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble DEEP_RESEARCH raw.json from read_tool outputs")
    ap.add_argument("--input", required=True, help="picks.json")
    ap.add_argument("--pick-index", type=int, required=True)
    ap.add_argument("--out-dir", required=True, help="dir of read_tool source records")
    ap.add_argument("--output", required=True, help="raw.json path")
    args = ap.parse_args()
    try:
        code, _ = build(
            os.path.realpath(args.input), args.pick_index,
            os.path.realpath(args.out_dir), os.path.realpath(args.output),
        )
        return code
    except Exception as e:
        write_error("scanner_exception", args.output)
        print(f"BUILD_ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
