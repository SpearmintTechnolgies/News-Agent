#!/usr/bin/env python3
"""
Pick article depth (4-6 H3s / FAQs) and word targets from research.json.

Depth 5/6 only when multi-source research passes hard data gates.
Word length is always 1100-1400 (aim 1250) regardless of depth.

Usage:
  python3 pick_article_structure.py /tmp/research.json [/tmp/article-structure.json]
"""

from __future__ import annotations

import json
import os
import sys

WORD_MIN = 1100
WORD_MAX = 1400
WORD_TARGET = 1250

# Per-depth budgets for writer <thinking> only (not separate word bands)
DEPTH_LAYOUT = {
    4: {"words_per_h3": 182, "words_per_faq": 60},
    5: {"words_per_h3": 163, "words_per_faq": 57},
    6: {"words_per_h3": 153, "words_per_faq": 54},
}

H2_LAYOUTS = {
    4: [2, 2],
    5: [2, 3],
    6: [2, 2, 2],
}

MIN_SCRAPE_DEPTH5 = 3500
MIN_SCRAPE_DEPTH6 = 5000
MIN_FACTS_DEPTH5 = 6
MIN_FACTS_DEPTH6 = 9


def _facts(data: dict) -> list[str]:
    raw = data.get("combined_key_facts") or []
    return [str(f).strip() for f in raw if str(f).strip()]


def _urls(data: dict) -> list[str]:
    return [str(u).strip() for u in (data.get("source_urls") or []) if str(u).strip()]


def _sources_used(data: dict) -> list[str]:
    return [str(s).strip() for s in (data.get("sources_used") or []) if str(s).strip()]


def _scrape_len(data: dict) -> int:
    return len(data.get("aggregated_raw_content") or "")


def qualifies_depth_5(data: dict) -> tuple[bool, str]:
    facts = _facts(data)
    urls = _urls(data)
    sources = _sources_used(data)
    scrape = _scrape_len(data)

    if len(urls) < 2:
        return False, "need_2_source_urls"
    if len(sources) < 2:
        return False, "need_2_sources_used"
    if len(facts) < MIN_FACTS_DEPTH5:
        return False, f"need_{MIN_FACTS_DEPTH5}_key_facts"
    if scrape < MIN_SCRAPE_DEPTH5:
        return False, "scrape_too_short_for_depth_5"
    spare = len(facts) - 4
    if spare < 2:
        return False, "need_2_spare_facts_for_extra_h3"
    return True, "ok"


def qualifies_depth_6(data: dict) -> tuple[bool, str]:
    ok5, reason5 = qualifies_depth_5(data)
    if not ok5:
        return False, f"depth5_gate_failed:{reason5}"

    facts = _facts(data)
    urls = _urls(data)
    scrape = _scrape_len(data)

    if len(urls) < 3:
        return False, "need_3_source_urls"
    if len(facts) < MIN_FACTS_DEPTH6:
        return False, f"need_{MIN_FACTS_DEPTH6}_key_facts"
    if scrape < MIN_SCRAPE_DEPTH6:
        return False, "scrape_too_short_for_depth_6"
    spare = len(facts) - 4
    if spare < 4:
        return False, "need_4_spare_facts_for_depth_6"
    return True, "ok"


def pick_depth(data: dict) -> tuple[int, bool, str]:
    override = os.environ.get("ARTICLE_DEPTH", "").strip()
    if override in ("4", "5", "6"):
        d = int(override)
        return d, d >= 5, f"env_override_{d}"

    ok6, r6 = qualifies_depth_6(data)
    if ok6:
        return 6, True, "rich_multi_source_9plus_facts"

    ok5, r5 = qualifies_depth_5(data)
    if ok5:
        return 5, True, "standard_multi_source_6_facts"

    facts = _facts(data)
    if len(facts) >= 4 and len(_urls(data)) >= 1:
        return 4, True, "baseline_depth_4_sufficient_for_standard_article"

    return 4, False, "insufficient_source_data"


def build_structure(data: dict) -> dict:
    depth, research_sufficient, depth_reason = pick_depth(data)
    layout = DEPTH_LAYOUT[depth]
    h2_layout = H2_LAYOUTS[depth]
    h2_count = len(h2_layout)
    h3_count = depth
    faq_count = depth

    return {
        "depth": depth,
        "h3_count": h3_count,
        "faq_count": faq_count,
        "h2_count": h2_count,
        "h2_layout": h2_layout,
        "research_sufficient": research_sufficient,
        "depth_reason": depth_reason,
        "word_min": WORD_MIN,
        "word_max": WORD_MAX,
        "word_target": WORD_TARGET,
        "words_per_h3": layout["words_per_h3"],
        "words_per_faq": layout["words_per_faq"],
        "words_hook": 120,
        "words_conclusion": 100,
        "faq_indices": list(range(1, faq_count + 1)),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: pick_article_structure.py <research.json> [output.json]", file=sys.stderr)
        return 1

    research_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/article-structure.json"

    with open(research_path, encoding="utf-8") as f:
        data = json.load(f)

    structure = build_structure(data)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2)

    print(
        f"STRUCTURE: depth={structure['depth']} h3={structure['h3_count']} "
        f"faq={structure['faq_count']} words={structure['word_min']}-{structure['word_max']} "
        f"reason={structure['depth_reason']} sufficient={structure['research_sufficient']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
