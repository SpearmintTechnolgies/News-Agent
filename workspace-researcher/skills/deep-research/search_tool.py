#!/usr/bin/env python3
"""
search_tool.py — Link discovery for Scout's DEEP_RESEARCH loop.

Primary: DuckDuckGo (ddgs html/lite). When DDG is empty, rate-limited, or
returns encyclopedia junk (common when a headline starts with a number),
fall back to Google News RSS and resolve aggregator URLs to publishers.

Usage:
  python3 search_tool.py --query "Binance Yi He impersonation scam CoinUp"
  python3 search_tool.py --query "..." --max 8 --timelimit m

Prints JSON to stdout:
  {"query": "...", "count": N, "results": [{"title","url","snippet","domain"}]}

stderr summary: SEARCH_OK: <n> results  |  SEARCH_EMPTY: <reason>
Exit 0 when >=1 result, 1 when none.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_RESEARCH_CHECK = os.path.join(os.path.dirname(_HERE), "research-check")
_PIPELINE = os.path.expanduser("~/.openclaw/workspace-orchestrator/skills/pipeline")
for _p in (_RESEARCH_CHECK, _PIPELINE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import resolve_url  # noqa: E402

# Domains that are never useful as a news source for extraction.
_SKIP_DOMAINS = {
    "news.google.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com", "mobile.twitter.com",
    "facebook.com", "m.facebook.com",
    "instagram.com", "tiktok.com", "pinterest.com",
    "linkedin.com", "t.me",
}

# DDG often returns these when the headline starts with a numeral ("6 New …").
_JUNK_DOMAINS = {
    "wikipedia.org", "en.wikipedia.org", "en.m.wikipedia.org",
    "britannica.com", "dictionary.com", "merriam-webster.com",
    "hotstar.com", "imdb.com", "amazon.com", "amazon.in",
    "coingecko.com", "coinmarketcap.com",
}

MAX_DEFAULT = 8
RETRIES = 3
RETRY_BACKOFF_S = 2.0
_GNEWS_TIMEOUT = 20
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _load_ddgs():
    """Import a DDGS class from either the new `ddgs` or legacy package."""
    try:
        from ddgs import DDGS  # type: ignore[import-untyped]
        return DDGS
    except Exception:
        pass
    try:
        from duckduckgo_search import DDGS  # type: ignore[import-untyped]
        return DDGS
    except Exception:
        return None


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _is_junk_domain(dom: str) -> bool:
    if not dom:
        return True
    for junk in _JUNK_DOMAINS:
        if dom == junk or dom.endswith("." + junk):
            return True
    return False


def _sanitize_query(query: str) -> str:
    """Drop a leading numeral so DDG does not search for '6' / '30%'."""
    q = (query or "").strip()
    q = re.sub(r"^\d+(?:\.\d+)?%?\s+", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q or (query or "").strip()


def _normalize_item(item: dict) -> dict | None:
    url = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
    if not url.startswith("http"):
        return None
    dom = _domain(url)
    if not dom or dom in _SKIP_DOMAINS or _is_junk_domain(dom):
        return None
    return {
        "title": str(item.get("title") or "").strip(),
        "url": url,
        "snippet": str(item.get("body") or item.get("snippet") or "").strip(),
        "domain": dom,
    }


def _priority_index(domain: str, order: list[str]) -> int:
    """Lower is better. Match a priority source name against the domain."""
    base = domain.split(".")[0] if domain else ""
    for i, name in enumerate(order):
        key = "".join(ch for ch in name.lower() if ch.isalnum())
        if key and (key in domain.replace(".", "") or key == base):
            return i
    return len(order) + 1


def _raw_search(ddgs_cls, query: str, max_results: int, timelimit: str | None) -> list[dict]:
    """Try multiple backends; return raw library result dicts."""
    backends = ["auto", "html", "lite"]
    last_err: Exception | None = None
    for backend in backends:
        for attempt in range(RETRIES):
            try:
                with ddgs_cls() as ddgs:
                    try:
                        results = list(ddgs.text(
                            query,
                            region="us-en",
                            safesearch="off",
                            timelimit=timelimit,
                            backend=backend,
                            max_results=max_results,
                        ))
                    except TypeError:
                        # Older signature without `backend`.
                        results = list(ddgs.text(
                            query,
                            region="us-en",
                            safesearch="off",
                            timelimit=timelimit,
                            max_results=max_results,
                        ))
                if results:
                    return results
            except Exception as e:  # ratelimit / network / backend errors
                last_err = e
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        # next backend
    if last_err:
        print(f"[search_tool] all backends failed: {last_err}", file=sys.stderr)
    return []


def _google_news_search(query: str, max_results: int) -> list[dict]:
    """Google News RSS → resolve wrappers to publisher URLs."""
    q = urllib.parse.quote_plus(query)
    rss = (
        "https://news.google.com/rss/search?q="
        + q
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    req = urllib.request.Request(rss, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_GNEWS_TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[search_tool] google-news rss failed: {e}", file=sys.stderr)
        return []

    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, UnicodeDecodeError) as e:
        print(f"[search_tool] google-news rss parse failed: {e}", file=sys.stderr)
        return []

    meta: list[tuple[str, str, str, str]] = []
    raw_urls: list[str] = []
    for it in root.findall(".//item")[: max_results * 2]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        snippet = (it.findtext("description") or "").strip()
        source_el = it.find("source")
        source = ((source_el.text or "") if source_el is not None else "").strip()
        if not link:
            continue
        raw_urls.append(link)
        meta.append((title, snippet or source, source, link))

    if not raw_urls:
        return []

    resolved_map = resolve_url.resolve_many(raw_urls, max_workers=4)
    out: list[dict] = []
    seen: set[str] = set()
    for title, snippet, _source, link in meta:
        resolved = resolved_map.get(link) or ""
        if not resolved or resolve_url.is_aggregator(resolved):
            continue
        norm = _normalize_item({
            "title": title,
            "url": resolved,
            "body": snippet,
        })
        if not norm or norm["domain"] in seen:
            continue
        seen.add(norm["domain"])
        out.append(norm)
        if len(out) >= max_results:
            break
    return out


def search(query: str, *, max_results: int = MAX_DEFAULT,
           timelimit: str | None = None,
           priority_order: list[str] | None = None) -> list[dict]:
    q = _sanitize_query(query)
    seen: set[str] = set()
    out: list[dict] = []

    ddgs_cls = _load_ddgs()
    if ddgs_cls is not None:
        raw = _raw_search(ddgs_cls, q, max_results * 3, timelimit)
        for item in raw:
            norm = _normalize_item(item)
            if not norm or norm["domain"] in seen:
                continue
            seen.add(norm["domain"])
            out.append(norm)
    else:
        print("[search_tool] ddgs library not importable; trying Google News", file=sys.stderr)

    if len(out) < 2:
        print(
            f"[search_tool] DDG usable={len(out)}; falling back to Google News RSS",
            file=sys.stderr,
        )
        for norm in _google_news_search(q, max_results):
            if norm["domain"] in seen:
                continue
            seen.add(norm["domain"])
            out.append(norm)

    order = priority_order or []
    if order:
        out.sort(key=lambda r: _priority_index(r["domain"], order))
    return out[:max_results]


def _load_priority_order() -> list[str]:
    try:
        import project_config as pc  # noqa: E402
        cfg = pc.load_project_config()
        order = cfg.get_path("research.source_priority_order", []) or []
        return [str(x) for x in order if x]
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="DuckDuckGo + Google News search for DEEP_RESEARCH")
    ap.add_argument("--query", required=True)
    ap.add_argument("--max", type=int, default=MAX_DEFAULT, help="max distinct-domain results")
    ap.add_argument("--timelimit", default=None,
                    help="d|w|m|y to restrict recency (default: none)")
    ap.add_argument("--no-priority", action="store_true",
                    help="do not reorder by project source_priority_order")
    args = ap.parse_args()

    order = [] if args.no_priority else _load_priority_order()
    results = search(args.query, max_results=args.max,
                     timelimit=args.timelimit, priority_order=order)

    print(json.dumps(
        {"query": args.query, "count": len(results), "results": results},
        indent=2, ensure_ascii=False,
    ))
    if results:
        print(f"SEARCH_OK: {len(results)} results", file=sys.stderr)
        return 0
    print("SEARCH_EMPTY: no usable results", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
