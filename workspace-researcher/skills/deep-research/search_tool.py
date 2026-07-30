#!/usr/bin/env python3
"""
search_tool.py — DuckDuckGo search for Scout's DEEP_RESEARCH loop.

The ONLY link-discovery tool. Given a query (usually the story headline), it
returns clean candidate publisher links the agent can read one-by-one with
read_tool.py. It calls the DuckDuckGo library directly (html/lite endpoints) —
it never scrapes the results page, so it does not hit Cloudflare or get blocked
the way curl/web_fetch from the home IP does.

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
import sys
import time
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_RESEARCH_CHECK = os.path.join(os.path.dirname(_HERE), "research-check")
_PIPELINE = os.path.expanduser("~/.openclaw/workspace-orchestrator/skills/pipeline")
for _p in (_RESEARCH_CHECK, _PIPELINE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Domains that are never useful as a news source for extraction.
_SKIP_DOMAINS = {
    "news.google.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com", "mobile.twitter.com",
    "facebook.com", "m.facebook.com",
    "instagram.com", "tiktok.com", "pinterest.com",
    "linkedin.com", "t.me",
}

MAX_DEFAULT = 8
RETRIES = 3
RETRY_BACKOFF_S = 2.0


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


def _normalize_item(item: dict) -> dict | None:
    url = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
    if not url.startswith("http"):
        return None
    dom = _domain(url)
    if not dom or dom in _SKIP_DOMAINS:
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


def search(query: str, *, max_results: int = MAX_DEFAULT,
           timelimit: str | None = None,
           priority_order: list[str] | None = None) -> list[dict]:
    ddgs_cls = _load_ddgs()
    if ddgs_cls is None:
        print("SEARCH_EMPTY: ddgs library not importable", file=sys.stderr)
        return []
    # Over-fetch so domain-dedupe still leaves enough candidates.
    raw = _raw_search(ddgs_cls, query, max_results * 3, timelimit)
    seen: set[str] = set()
    out: list[dict] = []
    for item in raw:
        norm = _normalize_item(item)
        if not norm:
            continue
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
    ap = argparse.ArgumentParser(description="DuckDuckGo search for DEEP_RESEARCH")
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
