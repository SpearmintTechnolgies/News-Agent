#!/usr/bin/env python3
"""
sync_article_from_raw.py — Sanitize article/raw.md → article/final.md
and enforce topic + source-URL coherence vs research/validated.json.

Usage:
    python3 sync_article_from_raw.py --manifest /path/to/manifest.json

Reads:   manifest.artifacts.article_raw
         manifest.artifacts.research_validated
Writes:  manifest.artifacts.article_final (atomic write)

Exit 0 + prints ARTICLE_SYNCED: <word_count> words
Exit 1 + prints ARTICLE_INVALID: <reason>  or  ARTICLE_STALE: <reason>
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import argparse
from urllib.parse import urlparse

# Writer contract (Quill SOUL / COINOGRAPHY): 1000–1200 body words.
# Orchestrator sync adds a tolerance buffer only — never tell the writer a higher max.
WRITER_WORD_MIN = 1000
WRITER_WORD_MAX = 1200
GATE_BUFFER_WORDS = 100  # sync passes up to WRITER_WORD_MAX + buffer before ARTICLE_INVALID


# ── helpers ──────────────────────────────────────────────────────────────────

def atomic_write(path: str, content: str) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False,
                                     suffix=".tmp", encoding="utf-8") as f:
        f.write(content)
        tmp = f.name
    os.replace(tmp, path)


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def strip_pty_noise(content: str) -> str:
    content = re.sub(r'\x1b\[[0-9;]*[mGKHFA-Za-z]', '', content)
    content = re.sub(r'<thinking>.*?</thinking>', '', content,
                     flags=re.DOTALL | re.IGNORECASE)
    for pat in [
        r'^Waiting for agent reply.*$',
        r'^Sending message.*$',
        r'^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏].*$',
    ]:
        content = re.sub(pat, '', content, flags=re.MULTILINE)
    content = re.sub(r'\n{3,}', '\n\n', content).strip()
    return content


def extract_h1(content: str) -> str:
    """Return the first # heading after the optional META block."""
    in_meta = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "META":
            in_meta = True
            continue
        if in_meta and stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def token_set(text: str) -> set[str]:
    """Lowercase word tokens, length ≥ 3, excluding stopwords."""
    STOP = {"the", "and", "for", "its", "are", "was", "has", "have",
            "with", "that", "this", "from", "not", "but", "can", "will",
            "all", "new", "law", "bill", "act"}
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in STOP}


def headline_matches(article_h1: str, research_headline: str) -> bool:
    """Return True if at least 2 meaningful tokens overlap."""
    a_tokens = token_set(article_h1)
    r_tokens = token_set(research_headline)
    overlap = a_tokens & r_tokens
    return len(overlap) >= 2


def topic_matches(article_h1: str, research: dict) -> bool:
    """Return True if H1 overlaps any of primary_keyword, topic_theme, or primary_headline.

    Checks in order of specificity: SEO keyword first (most likely to match writer output),
    then topic_theme, then the raw outlet headline. Any one >=2 token overlap is sufficient.
    """
    for field in ("primary_keyword", "topic_theme", "primary_headline"):
        val = research.get(field) or ""
        if val and headline_matches(article_h1, val):
            return True
    return False


SOURCES_FOOTER_START = re.compile(
    r"(?:\n---\s*)?(?:\n\*\*Sources:\*\*|\nSources:\s*\n|\n#{1,3}\s+Sources)\s*\n",
    re.IGNORECASE | re.MULTILINE,
)

WORD_COUNT_FOOTER_START = re.compile(
    r"\n#{1,3}\s+Word\s+Count\s*\n",
    re.IGNORECASE | re.MULTILINE,
)


def strip_publish_footers(content: str) -> str:
    """Remove Sources list and Word Count blocks for Drive/WP (keep in raw.md for validation)."""
    clean = content
    m = SOURCES_FOOTER_START.search(clean)
    if m:
        clean = clean[: m.start()]
    else:
        m2 = WORD_COUNT_FOOTER_START.search(clean)
        if m2:
            clean = clean[: m2.start()]
    clean = re.sub(
        r"^\[?Word Count:.*?\]?\s*$",
        "",
        clean,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    clean = re.sub(
        r"^#{1,3}\s+Word\s+Count\s*$",
        "",
        clean,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    clean = re.sub(
        r"^(?:Approx\.?\s*)?[\d,]+\s+words\s*$",
        "",
        clean,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return (clean + "\n") if clean else ""


def body_for_word_count(content: str) -> str:
    """Return content up to the Sources footer for word counting.

    Excludes Sources section, Word Count line, and any trailing metadata so
    the word-band check reflects article body length only, matching how
    footers are stripped before publishing.
    """
    m = SOURCES_FOOTER_START.search(content)
    return content[:m.start()] if m else content


def extract_body_urls(content: str) -> list[str]:
    """Return all http(s) URLs from markdown links in the article body,
    stopping at the Sources section."""
    SOURCES_SPLIT = re.compile(
        r"\n(?:\*\*Sources:\*\*|^Sources:|\n#{1,3}\s+Sources)\s*\n",
        re.IGNORECASE | re.MULTILINE,
    )
    parts = SOURCES_SPLIT.split(content, maxsplit=1)
    body = parts[0]
    return re.findall(r"\(https?://[^)]+\)", body)


def normalize_url(url: str) -> str:
    url = url.strip("()")
    p = urlparse(url)
    host = (p.netloc or "").lower().removeprefix("www.")
    path = p.path.rstrip("/") or ""
    return f"{p.scheme.lower()}://{host}{path}"


def url_matches_any(url: str, research_urls: list[str]) -> bool:
    norm = normalize_url(url)
    for ref in research_urls:
        ref_norm = normalize_url(ref)
        if norm == ref_norm:
            return True
        if norm.startswith(ref_norm) or ref_norm.startswith(norm):
            return True
        if norm.split("?")[0] == ref_norm.split("?")[0]:
            return True
    return False


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", help="Path to pipeline manifest.json")
    parser.add_argument("--run-dir", help="Run bundle directory (alternative to --manifest)")
    parser.add_argument(
        "--editorial",
        action="store_true",
        help="Editorial revision mode: freshness vs .revision_started",
    )
    args = parser.parse_args()

    if args.run_dir:
        run_dir = os.path.realpath(args.run_dir)
        raw_path = os.path.join(run_dir, "article", "raw.md")
        final_path = os.path.join(run_dir, "article", "final.md")
        validated_path = os.path.join(run_dir, "research", "validated.json")
        freshness_path = os.path.join(run_dir, ".revision_started" if args.editorial else ".run_started")
    elif args.manifest:
        manifest_path = os.path.realpath(args.manifest)
        if not os.path.exists(manifest_path):
            print(f"ARTICLE_INVALID: manifest not found: {manifest_path}")
            return 1
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError) as e:
            print(f"ARTICLE_INVALID: cannot parse manifest: {e}")
            return 1
        artifacts = manifest.get("artifacts", {})
        raw_path = artifacts.get("article_raw", "")
        final_path = artifacts.get("article_final", "")
        validated_path = artifacts.get("research_validated", "")
        run_dir = manifest.get("run_dir", "")
        freshness_path = os.path.join(run_dir, ".run_started")
    else:
        print("ARTICLE_INVALID: provide --manifest or --run-dir")
        return 1

    # ── 1. Check raw file exists and is fresh ─────────────────────────────
    if not raw_path or not os.path.exists(raw_path):
        print(f"ARTICLE_INVALID: article_raw not found: {raw_path}")
        return 1

    raw_stat = os.stat(raw_path)
    if raw_stat.st_size == 0:
        print("ARTICLE_INVALID: article/raw.md is empty — writer did not write the file")
        return 1

    if os.path.exists(freshness_path):
        try:
            with open(freshness_path) as f:
                started_epoch = float(f.read().strip())
            if raw_stat.st_mtime < started_epoch:
                stamp = os.path.basename(freshness_path)
                print(
                    f"ARTICLE_STALE: article/raw.md mtime is older than {stamp} — "
                    "writer did not update the file in this revision"
                )
                return 1
        except (ValueError, OSError):
            pass

    # ── 2. Read and sanitize raw ──────────────────────────────────────────
    with open(raw_path, "r", encoding="utf-8", errors="ignore") as f:
        raw_content = f.read()

    content = strip_pty_noise(raw_content)

    if not content.strip():
        print("ARTICLE_INVALID: article/raw.md is blank after sanitization")
        return 1

    # ── 3. Basic structural checks ────────────────────────────────────────
    errors: list[str] = []

    if re.search(r'\x1b\[', content):
        errors.append("ANSI codes still present after sanitize")
    if "Waiting for agent reply" in content:
        errors.append("PTY noise still present after sanitize")
    if not re.search(r'^# .+', content, re.MULTILINE):
        errors.append("No H1 title found")
    if not re.search(r'sources', content, re.IGNORECASE):
        errors.append("No Sources section")
    if not re.search(r'word count', content, re.IGNORECASE):
        errors.append("No Word Count block")

    # Gate = writer band + orchestrator buffer (never tell writer about buffer)
    word_min = WRITER_WORD_MIN
    word_max = WRITER_WORD_MAX + GATE_BUFFER_WORDS

    words = len(body_for_word_count(content).split())
    raw_words = len(content.split())
    if words < word_min:
        errors.append(
            f"Too short ({words} body words, writer min {WRITER_WORD_MIN})"
        )
    if words > word_max:
        errors.append(
            f"Too long ({words} body words; writer max {WRITER_WORD_MAX} + "
            f"{GATE_BUFFER_WORDS} buffer = gate {word_max}; full raw: {raw_words})"
        )

    if errors:
        print("ARTICLE_INVALID: " + "; ".join(errors))
        return 1

    # ── 4. Topic coherence: H1 vs research headline ───────────────────────
    if validated_path and os.path.exists(validated_path) and os.path.getsize(validated_path) > 0:
        try:
            research = load_json(validated_path)
            research_urls = research.get("source_urls", [])
        except (OSError, json.JSONDecodeError):
            research = {}
            research_urls = []

        if research:
            article_h1 = extract_h1(content)
            if article_h1 and not topic_matches(article_h1, research):
                print(f"ARTICLE_STALE: H1 topic mismatch\n"
                      f"  Article H1:       {article_h1[:80]}\n"
                      f"  primary_keyword:  {(research.get('primary_keyword') or '')[:80]}\n"
                      f"  topic_theme:      {(research.get('topic_theme') or '')[:80]}\n"
                      f"  primary_headline: {(research.get('primary_headline') or '')[:80]}\n"
                      f"  (less than 2 overlapping tokens in any research field)")
                return 1

        # ── 5. Source URL coherence: ≥1 body URL from research ───────────
        if research_urls:
            body_urls = extract_body_urls(content)
            matched = any(url_matches_any(u, research_urls) for u in body_urls)
            if body_urls and not matched:
                print(f"ARTICLE_STALE: no body source URL matches current research source_urls.\n"
                      f"  Body URLs found:    {body_urls[:3]}\n"
                      f"  Research URLs:      {research_urls[:3]}")
                return 1

    # ── 6. Strip internal footers, then write final.md ───────────────────
    if not final_path:
        final_path = os.path.join(run_dir, "article", "final.md")

    publish_content = strip_publish_footers(content)

    try:
        atomic_write(final_path, publish_content)
    except OSError as e:
        print(f"ARTICLE_INVALID: cannot write article/final.md: {e}")
        return 1

    publish_words = len(publish_content.split())
    print(f"ARTICLE_SYNCED: {publish_words} words (publish) → {final_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
