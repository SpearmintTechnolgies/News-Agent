#!/usr/bin/env python3
"""
check_article.py — Combined article validator (writer self-check + orchestrator gate).

Usage:
  python3 check_article.py --article /path/to/raw.md --research /path/to/validated.json
  python3 check_article.py --article /path/to/final.md --research /path/to/validated.json --post-sync

Prints a per-rule checklist (PASS / FAIL: fix hint) and ARTICLE_CHECK: PASS|FAIL.
Exit 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
_ARTICLE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ARTICLE_DIR not in sys.path:
    sys.path.insert(0, _ARTICLE_DIR)

import article_hygiene as hyg  # noqa: E402
import validate_article_structure as vas  # noqa: E402
import validate_anchor_links as val  # noqa: E402

# ── import sync helpers from orchestrator pipeline ─────────────────────────
_ORCH_PIPELINE = os.path.expanduser(
    "~/.openclaw/workspace-orchestrator/skills/pipeline"
)
if _ORCH_PIPELINE not in sys.path:
    sys.path.insert(0, _ORCH_PIPELINE)

import sync_article_from_raw as sync  # noqa: E402

WRITER_WORD_MIN = sync.WRITER_WORD_MIN
WRITER_WORD_MAX = sync.WRITER_WORD_MAX

BANNED_PHRASES = [
    "it's worth noting",
    "it is important to note",
    "delve into",
    "in conclusion",
    "furthermore",
    "moreover",
    "in summary",
    "the crypto landscape",
    "the world of crypto",
    "a testament to",
    "shed light on",
]

META_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(SEO Title|Meta Description|URL Slug|Primary Keyword):\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
WORD_COUNT_FOOTER_RE = re.compile(
    r"^\[Word Count:\s*(\d+)\s*\]\s*$",
    re.MULTILINE | re.IGNORECASE,
)
H1_RE = re.compile(r"^# .+", re.MULTILINE)


def load_content(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return sync.strip_pty_noise(f.read())


def load_research(path: str | None) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def parse_meta(content: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not content.strip().startswith("META"):
        return meta
    block_end = content.find("\n---")
    block = content[: block_end if block_end > 0 else len(content)]
    for m in META_LINE_RE.finditer(block):
        key = m.group(1).strip().lower().replace(" ", "_")
        meta[key] = m.group(2).strip()
    return meta


def body_after_h1_before_h2(content: str) -> str:
    """Hook text: after first H1, before first ##."""
    h1 = H1_RE.search(content)
    if not h1:
        return ""
    rest = content[h1.end() :]
    h2 = re.search(r"^##\s+", rest, re.MULTILINE)
    hook = rest[: h2.start()] if h2 else rest
    return hook.strip()


def first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    return parts[0]


def run_checks(
    content: str,
    research: dict,
    *,
    post_sync: bool,
) -> list[tuple[str, bool, str]]:
    """Return list of (rule_name, passed, detail)."""
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    body_for_count = sync.body_for_word_count(content)
    body_words = len(body_for_count.split())
    meta = parse_meta(content)
    primary_kw = (
        meta.get("primary_keyword")
        or research.get("primary_keyword")
        or ""
    ).strip()

    # ── structure ────────────────────────────────────────────────────────
    sources_parts = vas.SOURCES_SPLIT_RE.split(content, maxsplit=1)
    main = sources_parts[0]
    body, faq, _after = vas.split_parts(content)
    h3 = vas.count_h3(body)
    h2 = vas.count_h2_body(body)
    faq_n = vas.count_faqs(faq)

    for err in vas.check_section_order(main):
        record("section_order", False, err)
        break
    else:
        record("section_order", True, "Conclusion before FAQs")

    if vas.H3_MIN <= h3 <= vas.H3_MAX:
        record("h3_count", True, f"{h3} H3 subsections")
    else:
        record(
            "h3_count",
            False,
            f"add or adjust ### subsections — need {vas.H3_MIN}-{vas.H3_MAX}, found {h3}",
        )

    if vas.H2_MIN <= h2 <= vas.H2_MAX:
        record("h2_count", True, f"{h2} H2 body sections")
    else:
        record(
            "h2_count",
            False,
            f"adjust ## body sections — need {vas.H2_MIN}-{vas.H2_MAX}, found {h2}",
        )

    if vas.FAQ_MIN <= faq_n <= vas.FAQ_MAX:
        record("faq_count", True, f"{faq_n} FAQ items")
    else:
        record(
            "faq_count",
            False,
            f"add FAQ items as **N. Question?** — need {vas.FAQ_MIN}-{vas.FAQ_MAX}, found {faq_n}",
        )

    h1_matches = H1_RE.findall(content)
    if len(h1_matches) == 1:
        record("single_h1", True, "one H1 title")
    else:
        record(
            "single_h1",
            False,
            f"keep exactly one # H1 title (found {len(h1_matches)})",
        )

    # ── word band ──────────────────────────────────────────────────────────
    if WRITER_WORD_MIN <= body_words <= WRITER_WORD_MAX:
        record("word_band", True, f"{body_words} body words")
    elif body_words < WRITER_WORD_MIN:
        record(
            "word_band",
            False,
            f"expand body to {WRITER_WORD_MIN}-{WRITER_WORD_MAX} words (currently {body_words})",
        )
    else:
        record(
            "word_band",
            False,
            f"trim body to {WRITER_WORD_MIN}-{WRITER_WORD_MAX} words (currently {body_words}); do not cut Conclusion or FAQ answers",
        )

    # ── footers (raw only) ─────────────────────────────────────────────────
    if not post_sync:
        if vas.SOURCES_SPLIT_RE.search(content):
            record("sources_footer", True, "Sources block present")
        else:
            record(
                "sources_footer",
                False,
                "add **Sources:** block with publication URLs before Word Count line",
            )

        wc_match = WORD_COUNT_FOOTER_RE.search(content)
        if wc_match:
            footer_n = int(wc_match.group(1))
            if footer_n == body_words:
                record("word_count_footer", True, f"[Word Count: {footer_n}] matches body")
            else:
                record(
                    "word_count_footer",
                    False,
                    f"set footer to [Word Count: {body_words}] (footer says {footer_n})",
                )
        else:
            record(
                "word_count_footer",
                False,
                f"add final line [Word Count: {body_words}]",
            )

    # ── META limits (raw; optional on post-sync if META still present) ─────
    if meta and not post_sync:
        seo = meta.get("seo_title", "")
        desc = meta.get("meta_description", "")
        slug = meta.get("url_slug", "")

        if seo and len(seo) <= 55:
            record("meta_seo_title", True, f"{len(seo)} chars")
        elif seo:
            record("meta_seo_title", False, f"shorten SEO Title to ≤55 chars (currently {len(seo)})")
        else:
            record("meta_seo_title", False, "add SEO Title in META block")

        if desc and len(desc) <= 155:
            record("meta_description", True, f"{len(desc)} chars")
        elif desc:
            record(
                "meta_description",
                False,
                f"shorten Meta Description to ≤155 chars (currently {len(desc)})",
            )
        else:
            record("meta_description", False, "add Meta Description in META block")

        if slug and len(slug) <= 50:
            record("meta_url_slug", True, f"{len(slug)} chars")
        elif slug:
            record("meta_url_slug", False, f"shorten URL Slug to ≤50 chars (currently {len(slug)})")
        else:
            record("meta_url_slug", False, "add URL Slug in META block")

        if primary_kw and desc and primary_kw.lower() in desc.lower():
            record("meta_keyword_in_desc", True, "Primary Keyword in Meta Description")
        elif primary_kw and desc:
            record(
                "meta_keyword_in_desc",
                False,
                f"include Primary Keyword '{primary_kw}' verbatim in Meta Description",
            )

        if primary_kw and seo and primary_kw.lower() in seo.lower():
            record("meta_keyword_in_seo_title", True, "Primary Keyword in SEO Title")
        elif primary_kw and seo:
            record(
                "meta_keyword_in_seo_title",
                False,
                f"include Primary Keyword '{primary_kw}' in SEO Title (within first 3 words)",
            )
        elif primary_kw:
            record("meta_keyword_in_seo_title", False, "add SEO Title containing Primary Keyword")

        if primary_kw and slug and hyg.slug_contains_keyword(slug, primary_kw):
            record("meta_keyword_in_slug", True, "Primary Keyword tokens in URL Slug")
        elif primary_kw and slug:
            record(
                "meta_keyword_in_slug",
                False,
                f"URL Slug must include tokens from Primary Keyword '{primary_kw}'",
            )
        elif primary_kw:
            record("meta_keyword_in_slug", False, "add URL Slug containing Primary Keyword tokens")

    # ── hook / keyword ─────────────────────────────────────────────────────
    if primary_kw:
        h1_text = sync.extract_h1(content)
        if h1_text and primary_kw.lower() in h1_text.lower():
            record("h1_keyword", True, "Primary Keyword in H1")
        elif h1_text:
            record(
                "h1_keyword",
                False,
                f"H1 must contain Primary Keyword '{primary_kw}' (within first 5 words)",
            )
        else:
            record("h1_keyword", False, "add H1 containing Primary Keyword")

        hook = body_after_h1_before_h2(content)
        first = first_sentence(hook)
        if primary_kw.lower() in first.lower():
            record("primary_keyword_hook", True, "first sentence contains Primary Keyword")
        else:
            record(
                "primary_keyword_hook",
                False,
                f"first sentence under H1 must contain '{primary_kw}'",
            )

    # ── topic coherence ────────────────────────────────────────────────────
    if research:
        h1 = sync.extract_h1(content)
        if h1 and sync.topic_matches(h1, research):
            record("topic_coherence", True, "H1 matches research topic")
        elif h1:
            record(
                "topic_coherence",
                False,
                "restore H1 to match research primary_headline/primary_keyword — do not change the approved headline",
            )
        else:
            record("topic_coherence", False, "add H1 title matching research headline")

    # ── anchor links ───────────────────────────────────────────────────────
    body_text = val.body_before_sources(content)
    source_urls = val.extract_source_links(body_text)
    tweet_urls = val.extract_tweet_links(body_text)
    research_urls = research.get("source_urls") or []

    if tweet_urls:
        record(
            "anchor_no_tweets",
            False,
            "remove x.com/twitter.com links from body",
        )
    else:
        record("anchor_no_tweets", True, "no tweet links")

    if len(source_urls) <= 2:
        record("anchor_count", True, f"{len(source_urls)} in-body source links")
    else:
        record(
            "anchor_count",
            False,
            f"reduce in-body links to max 2 (found {len(source_urls)})",
        )

    norms = [val.normalize_url(u) for u in source_urls]
    if len(norms) == len(set(norms)):
        record("anchor_no_dupes", True, "distinct source URLs")
    else:
        record("anchor_no_dupes", False, "remove duplicate source URL in body")

    if research_urls:
        if not source_urls:
            record(
                "anchor_research_match",
                False,
                "add up to 2 markdown links to URLs from research source_urls in hook or first H2",
            )
        elif any(val.url_matches_research(u, research_urls) for u in source_urls):
            record("anchor_research_match", True, "links match research")
        else:
            record(
                "anchor_research_match",
                False,
                "use source URLs from validated.json source_urls only",
            )

    # ── output hygiene ───────────────────────────────────────────────────
    bad_headings = hyg.find_heading_inline_hash_lines(content)
    if bad_headings:
        record(
            "heading_no_inline_hash",
            False,
            "remove inline # markers inside heading text (use one clean heading line)",
        )
    else:
        record("heading_no_inline_hash", True, "headings have no inline hash runs")

    bare_sources = hyg.find_bare_source_lines(content, vas.SOURCES_SPLIT_RE)
    if bare_sources:
        record(
            "no_bare_source_line",
            False,
            "remove standalone source attribution lines; use inline anchor links in hook/first H2 only",
        )
    else:
        record("no_bare_source_line", True, "no bare source-only lines in body")

    # ── style ──────────────────────────────────────────────────────────────
    if "\u2014" in content or "—" in content:
        record("no_em_dash", False, "replace em-dashes with comma or period")
    else:
        record("no_em_dash", True, "no em-dashes")

    lower = content.lower()
    found_banned = [p for p in BANNED_PHRASES if p in lower]
    if found_banned:
        record(
            "banned_phrases",
            False,
            f"remove banned phrase(s): {', '.join(found_banned[:3])}",
        )
    else:
        record("banned_phrases", True, "no banned phrases")

    return results


def print_report(results: list[tuple[str, bool, str]]) -> bool:
    all_pass = True
    for name, ok, detail in results:
        if ok:
            print(f"PASS: {name} — {detail}")
        else:
            all_pass = False
            print(f"FAIL: {name} — {detail}")
    if all_pass:
        print("ARTICLE_CHECK: PASS")
    else:
        print("ARTICLE_CHECK: FAIL")
    return all_pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined article validator")
    parser.add_argument("--article", required=True, help="Path to article markdown")
    parser.add_argument("--research", default="", help="Path to validated.json")
    parser.add_argument(
        "--post-sync",
        action="store_true",
        help="Gate mode on final.md — skip raw-only footer checks",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.article):
        print(f"ARTICLE_CHECK: FAIL — file not found: {args.article}", file=sys.stderr)
        return 1

    content = load_content(args.article)
    if not content.strip():
        print("ARTICLE_CHECK: FAIL — article is empty", file=sys.stderr)
        return 1

    research = load_research(args.research or None)
    results = run_checks(content, research, post_sync=args.post_sync)
    ok = print_report(results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
