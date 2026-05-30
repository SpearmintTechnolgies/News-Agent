#!/usr/bin/env python3
"""search.py — Web search via ddgs with verified backend fallback."""
from __future__ import annotations

import hashlib
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ddgs import DDGS
from ddgs.exceptions import DDGSException

_ROOT = Path(__file__).resolve().parents[2]
_DB_DIR = _ROOT / "database"
if str(_DB_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_DIR))

import backlink_db  # noqa: E402

# Live-tested 2026-05-29 — never use duckduckgo or wikipedia (both return 0 results).
APPROVED_BACKENDS: tuple[str, ...] = ("google", "bing", "auto", "brave")
DEFAULT_LIMIT = 10
MAX_LIMIT = 25


class SearchError(Exception):
    """Raised when all approved backends fail for a query."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str = "google"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def cache_key(query: str, limit: int, backend: str) -> str:
    raw = f"{backend}:{query.strip().lower()}:{limit}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/") or ""
    return f"{parsed.scheme}://{parsed.netloc.lower()}{path}"


def normalize_results(raw: list[dict[str, Any]], *, source: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in raw:
        url = item.get("href") or item.get("url") or ""
        title = (item.get("title") or "").strip()
        snippet = (item.get("body") or item.get("snippet") or "").strip()
        if not url or not title:
            continue
        results.append(SearchResult(title=title, url=url, snippet=snippet, source=source))
    return results


def dedupe_by_url(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in results:
        key = normalize_url(result.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(result)
    return unique


def _search_backend(
    query: str,
    *,
    limit: int,
    backend: str,
) -> list[dict[str, Any]]:
    return list(DDGS().text(query, max_results=limit, backend=backend))


def search(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    use_cache: bool = True,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    backend: str | None = None,
) -> list[SearchResult]:
    query = query.strip()
    if not query:
        return []

    limit = max(1, min(limit, MAX_LIMIT))
    backends = (backend,) if backend else APPROVED_BACKENDS
    if backend and backend not in APPROVED_BACKENDS:
        raise SearchError(f"Backend {backend!r} is not approved for backlink discovery")

    last_error: str | None = None
    for candidate in backends:
        key = cache_key(query, limit, candidate)

        if use_cache:
            cached = backlink_db.get_search_cache(key, db_path=db_path)
            if cached is not None:
                return [SearchResult(**item) for item in cached]

        try:
            raw = _search_backend(query, limit=limit, backend=candidate)
        except DDGSException as exc:
            last_error = str(exc)
            continue

        if not raw:
            last_error = f"{candidate} returned 0 results"
            continue

        results = dedupe_by_url(normalize_results(raw, source=candidate))
        if not results:
            last_error = f"{candidate} returned no usable results"
            continue

        if use_cache:
            backlink_db.save_search_cache(
                key,
                query,
                candidate,
                [r.to_dict() for r in results],
                db_path=db_path,
            )
        return results

    raise SearchError(
        f"search returned 0 results for {query!r}"
        + (f": {last_error}" if last_error else "")
    )
