#!/usr/bin/env python3
"""
Validate in-body markdown link limits for crypto articles.

Rules:
  - At most 2 source anchor links (http(s) URLs from research sources)
  - No duplicate source URLs in body
  - No x.com / twitter.com status links in body
  - Sources footer and content below it are not scanned

Usage:
  python3 validate_anchor_links.py /tmp/crypto-article.md [/tmp/research.json]
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import urlparse

LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)", re.IGNORECASE)
SOURCES_SPLIT_RE = re.compile(
    r"\n(?:\*\*Sources:\*\*|^Sources:)\s*\n",
    re.IGNORECASE | re.MULTILINE,
)
TWEET_STATUS_RE = re.compile(r"/status/\d+", re.IGNORECASE)


def normalize_url(url: str) -> str:
    u = url.strip().rstrip(").,;")
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or ""
    return f"{parsed.scheme.lower()}://{host}{path}"


def is_tweet_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host not in ("x.com", "twitter.com"):
        return False
    return bool(TWEET_STATUS_RE.search(parsed.path or ""))


def body_before_sources(content: str) -> str:
    parts = SOURCES_SPLIT_RE.split(content, maxsplit=1)
    return parts[0]


def extract_source_links(content: str) -> list[str]:
    source_urls: list[str] = []
    for _label, url in LINK_RE.findall(content):
        if is_tweet_url(url):
            continue
        source_urls.append(url)
    return source_urls


def extract_tweet_links(content: str) -> list[str]:
    return [url for _label, url in LINK_RE.findall(content) if is_tweet_url(url)]


def url_matches_research(url: str, research_urls: list[str]) -> bool:
    norm = normalize_url(url)
    for ref in research_urls:
        ref_norm = normalize_url(ref)
        if norm == ref_norm or norm.startswith(ref_norm) or ref_norm.startswith(norm):
            return True
        if norm.split("?")[0] == ref_norm.split("?")[0]:
            return True
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: validate_anchor_links.py <article.md> [research.json]", file=sys.stderr)
        return 1

    article_path = sys.argv[1]
    research_path = sys.argv[2] if len(sys.argv) > 2 else None

    with open(article_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    body = body_before_sources(content)
    source_urls = extract_source_links(body)
    tweet_urls = extract_tweet_links(body)

    errors: list[str] = []
    warnings: list[str] = []

    if tweet_urls:
        errors.append(
            f"tweet links not allowed in article body ({len(tweet_urls)} found)"
        )

    if len(source_urls) > 2:
        errors.append(f"too many source anchor links ({len(source_urls)}, max 2)")

    norms = [normalize_url(u) for u in source_urls]
    if len(norms) != len(set(norms)):
        errors.append("duplicate source URL in body (link each source at most once)")

    research_urls: list[str] = []
    if research_path:
        try:
            with open(research_path, encoding="utf-8") as f:
                data = json.load(f)
            research_urls = data.get("source_urls") or []
        except (OSError, json.JSONDecodeError) as e:
            warnings.append(f"could not load research.json: {e}")

    if research_urls:
        matched_any = any(url_matches_research(u, research_urls) for u in source_urls)
        if source_urls and not matched_any:
            errors.append(
                "no source link in article body matches current research source_urls "
                "(article may be from a different story)"
            )

    if errors:
        print("ARTICLE_INVALID: " + "; ".join(errors))
        for w in warnings:
            print(f"[WARN]  {w}", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"[WARN]  {w}", file=sys.stderr)

    print(f"ANCHOR_LINKS: {len(source_urls)} source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
