from __future__ import annotations

import re
import sys
from html import unescape
from urllib.parse import urljoin, urlparse

import requests


def add(found, base, value):
    if not value:
        return

    value = unescape(value.strip())
    absolute = urljoin(base, value)

    if absolute not in found:
        found.append(absolute)


if len(sys.argv) != 2:
    print('Usage: python discover_feeds.py "https://example.com/page"')
    raise SystemExit(2)

url = sys.argv[1]

headers = {
    "User-Agent": (
        "NewsAgent-V2/0.1 "
        "(RSS discovery; contact: local-development)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

try:
    r = requests.get(
        url,
        headers=headers,
        timeout=15,
        allow_redirects=True,
    )

    print(f"HTTP: {r.status_code}")
    print(f"FINAL: {r.url}")
    print(f"TYPE: {r.headers.get('content-type', '')}")

    r.raise_for_status()

except Exception as exc:
    print(f"ERROR: {type(exc).__name__}: {exc}")
    raise SystemExit(1)

html = r.text
found = []

# 1. Explicit RSS/Atom <link> elements.
for tag in re.findall(r"<link\b[^>]*>", html, flags=re.I):
    if re.search(
        r'application/(?:rss|atom)\+xml',
        tag,
        flags=re.I,
    ):
        m = re.search(
            r'href=["\']([^"\']+)["\']',
            tag,
            flags=re.I,
        )
        if m:
            add(found, r.url, m.group(1))

# 2. Normal anchors whose URL looks feed-related.
for href in re.findall(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>',
    html,
    flags=re.I,
):
    lower = href.lower()

    if any(
        marker in lower
        for marker in (
            "rss",
            "feed",
            ".xml",
            "atom",
        )
    ):
        add(found, r.url, href)

# 3. Anchors whose visible text says RSS/feed.
for match in re.finditer(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    html,
    flags=re.I | re.S,
):
    href, body = match.groups()

    text = re.sub(r"<[^>]+>", " ", body)
    text = unescape(text)

    if re.search(r"\b(rss|atom|feed)\b", text, flags=re.I):
        add(found, r.url, href)

print(f"DISCOVERED: {len(found)}")

for candidate in found:
    print(candidate)
