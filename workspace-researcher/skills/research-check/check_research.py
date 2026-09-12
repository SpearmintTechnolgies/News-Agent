#!/usr/bin/env python3
"""
check_research.py — Combined research validator (Scout self-check + orchestrator gate).

Single source of truth for both research modes. The orchestrator's
validate_research.py and validate_headlines.py import the check functions from
this module so the agent self-check and the orchestrator gates never diverge —
exactly like check_article.py for the writer.

Usage:
  python3 check_research.py --file /path/to/raw.json --mode deep_research
  python3 check_research.py --file /path/to/headlines.json --mode headline_scan

Prints a per-rule checklist (PASS / FAIL: fix hint) and RESEARCH_CHECK: PASS|FAIL.
Exit 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

# ── shared constants (imported by the orchestrator validators) ──────────────
RESEARCH_REQUIRED_FIELDS = [
    "story_id",
    "primary_headline",
    "topic_theme",
    "combined_key_facts",
    "source_urls",
    "chart_coin",
]

HEADLINE_REQUIRED_PER_CANDIDATE = ["headline", "url", "source"]

# Clean error reasons Scout is allowed to hand back instead of garbage.
KNOWN_ERROR_REASONS = {
    "pick_index_not_found",
    "all_urls_dead",
    "url_unresolvable",
}

PROSE_MIN_WORDS = 600
MIN_SOURCES = 2

# Markers that mean "this is an internal trajectory/system log, not research".
_LOG_MARKERS = (
    "traceSchema",
    "openclaw-trajectory",
    "assistantTexts",
    "trajectoryId",
    "toolCallId",
    "\"role\":\"assistant\"",
)


def extract_json_block(text: str) -> tuple[dict | None, str | None]:
    """Mirror the orchestrator's lenient extraction: first { .. last }."""
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None, "no JSON object found"
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    if not isinstance(data, dict):
        return None, "top-level JSON is not an object"
    return data, None


def looks_like_log(text: str) -> bool:
    if any(m in text for m in _LOG_MARKERS):
        return True
    # A research file has a handful of "type" keys at most; a trajectory log
    # has hundreds of repeated `"type":` log lines.
    return text.count('"type"') > 50


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:2000].lower()
    if head.startswith("<!doctype") or head.startswith("<html") or "<head" in head[:600]:
        return True
    tags = len(re.findall(r"<[a-z/!][^>]+>", text[:20000], re.IGNORECASE))
    return tags > 80


def is_aggregator_url(url: str) -> bool:
    """True for unresolved aggregator wrappers (resolve_url.py should fix these)."""
    u = (url or "").lower()
    if "news.google.com" in u:
        return True
    if "/rss/articles/" in u:
        return True
    return False


def _markup_ratio(text: str) -> float:
    if not text:
        return 0.0
    tags = len(re.findall(r"<[a-z/!][^>]+>", text, re.IGNORECASE))
    return tags / max(len(text.split()), 1)


def _garbage_guards(raw_text: str) -> list[tuple[str, bool, str]]:
    """Catch the exact incident: log dumps and raw HTML dumps. Empty list = clean."""
    results: list[tuple[str, bool, str]] = []
    if looks_like_log(raw_text):
        results.append((
            "not_a_log",
            False,
            "output is an internal trajectory/system log, not research JSON — "
            "write the research object (or a clean error JSON) instead",
        ))
    if looks_like_html(raw_text):
        results.append((
            "not_raw_html",
            False,
            "output is raw HTML, not research JSON — extract article text with "
            "trafilatura/web-reader-pro, or write a clean error JSON",
        ))
    if not results and len(raw_text) > 60000:
        # Huge file that still has no parseable JSON object handled by caller;
        # flag the absurd-size case explicitly.
        data, err = extract_json_block(raw_text)
        if data is None:
            results.append((
                "size_sane",
                False,
                f"{len(raw_text)} chars with no parseable JSON object ({err}) — "
                "do not dump scrape output into the file",
            ))
    return results


def run_deep_research_checks(
    data: dict | None, raw_text: str
) -> list[tuple[str, bool, str]]:
    """Full DEEP_RESEARCH checklist. Returns list of (rule, passed, detail)."""
    results: list[tuple[str, bool, str]] = []

    guards = _garbage_guards(raw_text)
    if guards:
        return guards

    if data is None:
        data, err = extract_json_block(raw_text)
    if data is None:
        return [("parseable_json", False, "no parseable JSON object found in output")]
    results.append(("parseable_json", True, "single JSON object"))

    if data.get("status") == "partial":
        partial_words = int(data.get("partial_words") or 0)
        results.append((
            "not_partial",
            False,
            f"research is partial ({partial_words} words) — re-run run_research.py "
            "with --extra-search before yielding SUCCESS",
        ))
        return results

    # Clean error path is a valid, accepted outcome.
    if data.get("status") == "error":
        reason = str(data.get("reason") or "").strip()
        partial_words = int(data.get("partial_words") or 0)
        if partial_words > 0:
            results.append((
                "no_premature_error",
                False,
                f"partial extraction ({partial_words} words) — re-run run_research.py "
                "with --extra-search; do not emit clean error JSON while content exists",
            ))
            return results
        if reason in KNOWN_ERROR_REASONS:
            results.append((
                "clean_error_json",
                True,
                f"error-json ({reason}) — orchestrator will skip the story cleanly",
            ))
            return results
        results.append((
            "clean_error_json",
            False,
            f"status=error needs a known reason {sorted(KNOWN_ERROR_REASONS)} (got '{reason}')",
        ))
        return results

    missing = []
    for k in RESEARCH_REQUIRED_FIELDS:
        if k == "chart_coin":
            # Present key with empty string is valid (Unknown / no chartable asset).
            if "chart_coin" not in data or data.get("chart_coin") is None:
                missing.append(k)
            continue
        if not data.get(k):
            missing.append(k)
    if missing:
        results.append(("required_fields", False, f"missing/empty fields: {missing}"))
    else:
        results.append(("required_fields", True, "all required fields present"))

    facts = data.get("combined_key_facts") or []
    if isinstance(facts, list) and len(facts) >= 2:
        results.append(("key_facts", True, f"{len(facts)} key facts"))
    else:
        results.append((
            "key_facts",
            False,
            f"combined_key_facts needs >= 2 entries (got {len(facts) if isinstance(facts, list) else 'non-list'})",
        ))

    urls = data.get("source_urls")
    if isinstance(urls, list) and urls:
        results.append(("source_urls_present", True, f"{len(urls)} source URLs"))
    else:
        results.append((
            "source_urls_present",
            False,
            "source_urls must be a non-empty list",
        ))
        urls = []

    source_count = len(urls) if isinstance(urls, list) else 0
    if source_count >= MIN_SOURCES:
        results.append(("multi_source", True, f"{source_count} distinct source URLs"))
    else:
        results.append((
            "multi_source",
            False,
            f"source_urls needs >= {MIN_SOURCES} entries (got {source_count}) — "
            "re-run run_research.py with --extra-search",
        ))

    aggregator_hits = [u for u in urls if isinstance(u, str) and is_aggregator_url(u)]
    if aggregator_hits:
        results.append((
            "source_urls_resolved",
            False,
            "source_urls still contain an unresolved aggregator wrapper "
            f"({aggregator_hits[0]}) — run resolve_url.py and store the publisher URL",
        ))
    else:
        results.append(("source_urls_resolved", True, "publisher URLs (no aggregator wrappers)"))

    prose = str(data.get("aggregated_raw_content") or "")
    words = len(prose.split())
    ratio = _markup_ratio(prose)
    if words >= PROSE_MIN_WORDS and ratio < 0.05:
        results.append(("prose_quality", True, f"{words} words, clean prose"))
    elif words < PROSE_MIN_WORDS:
        results.append((
            "prose_quality",
            False,
            f"aggregated_raw_content is only {words} words — need >= {PROSE_MIN_WORDS} "
            "of substantive article text",
        ))
    else:
        results.append((
            "prose_quality",
            False,
            "aggregated_raw_content looks like markup, not prose — scrape the "
            "article body, not the page HTML",
        ))

    return results


def run_headline_scan_checks(
    data: dict | None, raw_text: str
) -> list[tuple[str, bool, str]]:
    """HEADLINE_SCAN checklist. Returns list of (rule, passed, detail)."""
    results: list[tuple[str, bool, str]] = []

    guards = _garbage_guards(raw_text)
    if guards:
        return guards

    if data is None:
        data, err = extract_json_block(raw_text)
    if data is None:
        return [("parseable_json", False, "no parseable JSON object found in output")]
    results.append(("parseable_json", True, "single JSON object"))

    if data.get("status") == "error":
        reason = str(data.get("reason") or "").strip()
        if reason in KNOWN_ERROR_REASONS or reason:
            results.append(("clean_error_json", True, f"error-json ({reason or 'no candidates'})"))
            return results

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        results.append(("candidates_present", False, "candidates must be a non-empty list"))
        return results
    results.append(("candidates_present", True, f"{len(candidates)} candidates"))

    bad_fields: list[int] = []
    aggregator_hits: list[str] = []
    for idx, c in enumerate(candidates, 1):
        if not isinstance(c, dict) or any(not c.get(k) for k in HEADLINE_REQUIRED_PER_CANDIDATE):
            bad_fields.append(idx)
            continue
        url = str(c.get("url") or "")
        if is_aggregator_url(url):
            aggregator_hits.append(url)

    if bad_fields:
        results.append((
            "candidate_fields",
            False,
            f"candidates missing {HEADLINE_REQUIRED_PER_CANDIDATE} at index(es): {bad_fields[:5]}",
        ))
    else:
        results.append(("candidate_fields", True, "every candidate has headline/url/source"))

    if aggregator_hits:
        results.append((
            "candidate_urls_resolved",
            False,
            f"candidate URL still an aggregator wrapper ({aggregator_hits[0]}) — "
            "run resolve_url.py at scan time",
        ))
    else:
        results.append(("candidate_urls_resolved", True, "publisher URLs (no aggregator wrappers)"))

    return results


def validate_file(path: str, mode: str) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Read a file and run the mode's checks. Returns (all_passed, results)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw_text = f.read()
    except OSError as e:
        return False, [("file_readable", False, f"cannot read file: {e}")]

    if not raw_text.strip():
        return False, [("not_empty", False, "output file is empty")]

    data, _ = extract_json_block(raw_text)
    if mode == "headline_scan":
        results = run_headline_scan_checks(data, raw_text)
    else:
        results = run_deep_research_checks(data, raw_text)
    return all(ok for _, ok, _ in results), results


def print_report(results: list[tuple[str, bool, str]]) -> bool:
    all_pass = True
    for name, ok, detail in results:
        if ok:
            print(f"PASS: {name} — {detail}")
        else:
            all_pass = False
            print(f"FAIL: {name} — {detail}")
    print(f"RESEARCH_CHECK: {'PASS' if all_pass else 'FAIL'}")
    return all_pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined research validator")
    parser.add_argument("--file", required=True, help="Path to the research JSON output")
    parser.add_argument(
        "--mode",
        choices=["deep_research", "headline_scan"],
        default="deep_research",
        help="Which mode's checks to run (default deep_research)",
    )
    args = parser.parse_args()

    ok, results = validate_file(args.file, args.mode)
    print_report(results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
