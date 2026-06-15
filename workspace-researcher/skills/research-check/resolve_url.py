#!/usr/bin/env python3
"""
resolve_url.py — Resolve Google News / aggregator wrapper URLs to the real
publisher URL so deep extraction scrapes the article, not a loading page.

Usage:
  python3 resolve_url.py "<url>"
  echo "url1\nurl2" | python3 resolve_url.py --batch

Prints the resolved publisher URL on stdout (exit 0), or:
  RESOLVE_FAILED: <reason>   (exit 1)

Batch mode prints one line per input URL: resolved URL or RESOLVE_FAILED.

Non-aggregator URLs are echoed back unchanged. Stdlib only (urllib) so it runs
anywhere the researcher does.

Strategy (cheapest first):
  1. Not an aggregator host -> echo unchanged.
  2. Follow plain HTTP redirects (handles already-301'd wrappers).
  3. Decode news.google.com/rss/articles/CBM... via Google's batchexecute
     (DotsSplashUi) endpoint to recover the canonical publisher URL.

Disk cache (~7 day TTL) avoids repeat network calls. One retry with backoff
on transient failures.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock

TIMEOUT = 15
_RETRY_BACKOFF_S = 0.5
_CACHE_TTL_DAYS = 7
_CACHE_PATH = os.path.expanduser("~/.openclaw/data/resolve_cache.json")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_AGGREGATOR_HOSTS = ("news.google.com",)
_GN_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

_cache_lock = Lock()


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""


def is_aggregator(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in _AGGREGATOR_HOSTS)


def _load_cache() -> dict:
    if not os.path.isfile(_CACHE_PATH):
        return {}
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, _CACHE_PATH)
    except OSError:
        pass


def _cache_get(url: str) -> str | None:
    with _cache_lock:
        cache = _load_cache()
        entry = cache.get(url)
        if not isinstance(entry, dict):
            return None
        resolved = entry.get("resolved")
        ts = entry.get("ts")
        if not resolved or not ts:
            return None
        try:
            age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 86400
        except ValueError:
            return None
        if age_days > _CACHE_TTL_DAYS:
            return None
        return str(resolved)


def _cache_set(url: str, resolved: str | None) -> None:
    with _cache_lock:
        cache = _load_cache()
        cache[url] = {
            "resolved": resolved,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _save_cache(cache)


def _get(url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[str, str]:
    """Return (final_url, body_text). Follows redirects by default."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.geturl(), body


def _get_with_retry(url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[str, str]:
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            return _get(url, data=data, headers=headers)
        except (urllib.error.URLError, ValueError, OSError) as e:
            last_err = e
            if attempt == 0:
                time.sleep(_RETRY_BACKOFF_S)
    raise last_err  # type: ignore[misc]


def _follow_redirect(url: str) -> str | None:
    """If following redirects lands off the aggregator host, return that URL."""
    try:
        final_url, _ = _get_with_retry(url)
    except (urllib.error.URLError, ValueError, OSError):
        return None
    if final_url and not is_aggregator(final_url):
        return final_url
    return None


def _gn_article_id(url: str) -> str | None:
    """Extract the CBM... id segment from a news.google.com articles URL."""
    path = urllib.parse.urlparse(url).path
    m = re.search(r"/(?:rss/)?(?:articles|read)/([^/?#]+)", path)
    return m.group(1) if m else None


def _decode_google_news(url: str) -> str | None:
    """Decode a news.google.com/rss/articles/CBM... wrapper via batchexecute."""
    art_id = _gn_article_id(url)
    if not art_id:
        return None

    try:
        _, html = _get_with_retry(f"https://news.google.com/rss/articles/{art_id}")
    except (urllib.error.URLError, ValueError, OSError):
        return None

    sig = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not sig or not ts:
        return None
    signature, timestamp = sig.group(1), ts.group(1)

    inner = json.dumps([
        "garturlreq",
        [
            ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
        ],
        art_id,
        int(timestamp),
        signature,
    ])
    freq = json.dumps([[["Fbv4je", inner]]])
    payload = urllib.parse.urlencode({"f.req": freq}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}

    try:
        _, body = _get_with_retry(_GN_BATCH_URL, data=payload, headers=headers)
    except (urllib.error.URLError, ValueError, OSError):
        return None

    for chunk in body.split("\n"):
        chunk = chunk.strip()
        if not chunk.startswith("[["):
            continue
        try:
            outer = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        for row in outer:
            if len(row) >= 3 and isinstance(row[2], str) and "http" in row[2]:
                try:
                    inner_arr = json.loads(row[2])
                except json.JSONDecodeError:
                    continue
                if isinstance(inner_arr, list) and len(inner_arr) >= 2 and isinstance(inner_arr[1], str):
                    if inner_arr[1].startswith("http"):
                        return inner_arr[1]
    return None


def _resolve_uncached(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if not is_aggregator(url):
        return url
    return _follow_redirect(url) or _decode_google_news(url)


def resolve(url: str, *, use_cache: bool = True) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if not is_aggregator(url):
        return url
    if use_cache:
        cached = _cache_get(url)
        if cached and not is_aggregator(cached):
            return cached
    resolved = _resolve_uncached(url)
    if use_cache:
        _cache_set(url, resolved)
    return resolved


def resolve_many(urls: list[str], *, max_workers: int = 8, use_cache: bool = True) -> dict[str, str | None]:
    """Resolve a list of URLs in parallel. Returns {input_url: resolved_or_none}."""
    unique = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    out: dict[str, str | None] = {}

    def _one(u: str) -> tuple[str, str | None]:
        return u, resolve(u, use_cache=use_cache)

    if len(unique) <= 1:
        for u in unique:
            out[u] = resolve(u, use_cache=use_cache)
        return out

    with ThreadPoolExecutor(max_workers=min(max_workers, len(unique))) as pool:
        futures = {pool.submit(_one, u): u for u in unique}
        for fut in as_completed(futures):
            u, res = fut.result()
            out[u] = res
    return out


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--batch":
        urls = [line.strip() for line in sys.stdin if line.strip()]
        results = resolve_many(urls)
        for u in urls:
            resolved = results.get(u)
            if resolved and not is_aggregator(resolved):
                print(resolved)
            else:
                print("RESOLVE_FAILED")
        return 0

    if len(sys.argv) != 2:
        print("RESOLVE_FAILED: usage: resolve_url.py <url> | resolve_url.py --batch", file=sys.stderr)
        return 1
    resolved = resolve(sys.argv[1])
    if resolved and not is_aggregator(resolved):
        print(resolved)
        return 0
    print("RESOLVE_FAILED: could not resolve aggregator wrapper to a publisher URL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
