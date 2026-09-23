"""Collector V2 - bounded-concurrency RSS collector with per-source error isolation.

One broken source MUST NOT kill /make.
Supports a run-scoped feed cache so each feed URL is fetched at most once per
discovery/Top-5 run (shared across initial collect + dimension-gap expansion).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import feedparser

from .normalizer import NewsNormalizer
from .raw_news_item import RawNewsItem
from .source_registry import SourceMetadata

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 8
DEFAULT_REQUEST_TIMEOUT = 12
DEFAULT_MAX_RETRIES = 1
USER_AGENT = "NewsAgentV2Collector/2.0 (+https://coinnetwork.local)"


@dataclass
class SourceResult:
    """Result of collecting from a single source."""
    source_id: str
    success: bool
    items_collected: int
    items_normalized: int
    error: str | None = None
    latency_ms: float = 0.0
    feed_entries: int = 0
    cache_hit: bool = False


@dataclass
class CollectorDiagnostics:
    """Diagnostics from a collection run."""
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    raw_items_collected: int = 0
    raw_items_normalized: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    source_results: list[SourceResult] = field(default_factory=list)
    total_latency_ms: float = 0.0
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources_attempted": self.sources_attempted,
            "sources_succeeded": self.sources_succeeded,
            "sources_failed": self.sources_failed,
            "raw_items_collected": self.raw_items_collected,
            "raw_items_normalized": self.raw_items_normalized,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "total_latency_ms": self.total_latency_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "errors": self.errors,
            "source_results": [
                {
                    "source_id": r.source_id,
                    "success": r.success,
                    "items_collected": r.items_collected,
                    "items_normalized": r.items_normalized,
                    "error": r.error,
                    "latency_ms": round(r.latency_ms, 2),
                    "cache_hit": r.cache_hit,
                }
                for r in self.source_results
            ],
        }


def _cache_key(source: SourceMetadata) -> str:
    return str(source.url or "").strip().lower().rstrip("/")


class CollectorV2:
    """Collector V2 with bounded concurrency, run-scoped cache, and error isolation.

    Features:
    - Bounded concurrency (configurable max workers)
    - Per-request network timeouts (no process-global socket default)
    - Safe retries with backoff
    - Per-source error isolation
    - Run-scoped URL cache (fetch each feed at most once per /make discovery)
    - Detailed diagnostics
    """

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        per_feed_limit: int = 30,
        request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        enable_run_cache: bool = True,
    ):
        self.max_workers = max(1, int(max_workers))
        self.per_feed_limit = per_feed_limit
        self.request_timeout = max(1, int(request_timeout))
        self.max_retries = max(0, int(max_retries))
        self.enable_run_cache = bool(enable_run_cache)
        self.normalizer = NewsNormalizer()
        self.diagnostics = CollectorDiagnostics()
        self._run_cache: dict[str, tuple[list[RawNewsItem], SourceResult]] = {}
        self._cache_lock = Lock()
        self._in_flight: dict[str, Lock] = {}
        self._in_flight_guard = Lock()

    def begin_run(self) -> None:
        """Clear run-scoped cache at the start of a /make discovery."""
        with self._cache_lock:
            self._run_cache.clear()
        with self._in_flight_guard:
            self._in_flight.clear()

    def clear_run_cache(self) -> None:
        self.begin_run()

    def cache_stats(self) -> dict[str, int]:
        with self._cache_lock:
            return {"cached_feeds": len(self._run_cache)}

    def _timeout_for(self, source: SourceMetadata) -> int:
        src_t = int(getattr(source, "timeout_seconds", 0) or 0)
        if src_t > 0:
            return max(1, min(self.request_timeout, src_t))
        return self.request_timeout

    def _download_feed_bytes(self, url: str, timeout: int) -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        with urlopen(request, timeout=timeout) as response:
            # Cap body size to avoid pathological feeds.
            return response.read(2_000_000)

    def _normalize_entries(
        self,
        source: SourceMetadata,
        parsed: Any,
    ) -> tuple[list[RawNewsItem], int, int]:
        feed_entries = len(getattr(parsed, "entries", []) or [])
        accepted = 0
        items: list[RawNewsItem] = []
        source_meta = {
            "name": source.name,
            "source_id": source.source_id,
            "source_type": source.source_type,
            "role": source.role,
            "authority": source.authority,
        }
        for entry in list(parsed.entries)[: self.per_feed_limit]:
            try:
                item = self.normalizer.normalize_item(
                    {
                        "title": entry.get("title"),
                        "link": entry.get("link") or entry.get("url"),
                        "summary": entry.get("summary") or entry.get("description"),
                        "published": entry.get("published") or entry.get("updated"),
                        "source": source.name,
                    },
                    source_meta,
                )
                items.append(item)
                accepted += 1
            except Exception:
                continue
        return items, feed_entries, accepted

    def _fetch_source_uncached(
        self,
        source: SourceMetadata,
    ) -> tuple[list[RawNewsItem], SourceResult]:
        """Fetch and normalize items from a single source (network)."""
        start_time = time.perf_counter()
        items: list[RawNewsItem] = []
        timeout = self._timeout_for(source)
        last_error = "unknown"

        for attempt in range(self.max_retries + 1):
            try:
                raw = self._download_feed_bytes(source.url, timeout=timeout)
                parsed = feedparser.parse(raw)
                items, feed_entries, accepted = self._normalize_entries(source, parsed)
                latency_ms = (time.perf_counter() - start_time) * 1000
                result = SourceResult(
                    source_id=source.source_id,
                    success=True,
                    items_collected=feed_entries,
                    items_normalized=accepted,
                    latency_ms=latency_ms,
                    feed_entries=feed_entries,
                    cache_hit=False,
                )
                return items, result
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:200]}"
                if attempt == self.max_retries:
                    latency_ms = (time.perf_counter() - start_time) * 1000
                    result = SourceResult(
                        source_id=source.source_id,
                        success=False,
                        items_collected=0,
                        items_normalized=0,
                        error=last_error,
                        latency_ms=latency_ms,
                        cache_hit=False,
                    )
                    return items, result
                time.sleep(0.25 * (attempt + 1))

        latency_ms = (time.perf_counter() - start_time) * 1000
        return items, SourceResult(
            source_id=source.source_id,
            success=False,
            items_collected=0,
            items_normalized=0,
            error=last_error or "max_retries_exceeded",
            latency_ms=latency_ms,
            cache_hit=False,
        )

    def _fetch_source(
        self,
        source: SourceMetadata,
    ) -> tuple[list[RawNewsItem], SourceResult]:
        """Fetch with optional run-scoped cache (one network fetch per feed URL)."""
        key = _cache_key(source)
        if self.enable_run_cache and key:
            with self._cache_lock:
                hit = self._run_cache.get(key)
            if hit is not None:
                items, result = hit
                cached_result = SourceResult(
                    source_id=source.source_id,
                    success=result.success,
                    items_collected=result.items_collected,
                    items_normalized=result.items_normalized,
                    error=result.error,
                    latency_ms=0.0,
                    feed_entries=result.feed_entries,
                    cache_hit=True,
                )
                # Return shallow-copied item list so callers can mutate freely.
                return list(items), cached_result

            # Single-flight: only one worker fetches a given URL.
            with self._in_flight_guard:
                lock = self._in_flight.get(key)
                if lock is None:
                    lock = Lock()
                    self._in_flight[key] = lock
            with lock:
                with self._cache_lock:
                    hit = self._run_cache.get(key)
                if hit is not None:
                    items, result = hit
                    cached_result = SourceResult(
                        source_id=source.source_id,
                        success=result.success,
                        items_collected=result.items_collected,
                        items_normalized=result.items_normalized,
                        error=result.error,
                        latency_ms=0.0,
                        feed_entries=result.feed_entries,
                        cache_hit=True,
                    )
                    return list(items), cached_result
                items, result = self._fetch_source_uncached(source)
                with self._cache_lock:
                    self._run_cache[key] = (list(items), result)
                return list(items), result

        return self._fetch_source_uncached(source)

    def collect(
        self,
        sources: list[SourceMetadata],
    ) -> tuple[list[RawNewsItem], CollectorDiagnostics]:
        """Collect from all sources with bounded concurrency.

        Returns:
            Tuple of (all_items, diagnostics)
        """
        self.diagnostics = CollectorDiagnostics(
            sources_attempted=len(sources),
            started_at=datetime.utcnow().isoformat(),
        )

        all_items: list[RawNewsItem] = []
        total_start = time.perf_counter()
        # Bound wait so a stuck worker cannot hang forever.
        per_future_timeout = float(self.request_timeout * (self.max_retries + 1) + 5)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_source = {
                executor.submit(self._fetch_source, source): source
                for source in sources
            }
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    items, result = future.result(timeout=per_future_timeout)
                    all_items.extend(items)
                    self.diagnostics.source_results.append(result)
                    if result.cache_hit:
                        self.diagnostics.cache_hits += 1
                    else:
                        self.diagnostics.cache_misses += 1

                    if result.success:
                        self.diagnostics.sources_succeeded += 1
                        self.diagnostics.raw_items_collected += result.items_collected
                        self.diagnostics.raw_items_normalized += result.items_normalized
                    else:
                        self.diagnostics.sources_failed += 1
                        self.diagnostics.errors.append({
                            "source_id": source.source_id,
                            "error": result.error,
                        })
                except FuturesTimeout:
                    self.diagnostics.sources_failed += 1
                    self.diagnostics.cache_misses += 1
                    self.diagnostics.errors.append({
                        "source_id": source.source_id,
                        "error": f"future_timeout_after_{per_future_timeout}s",
                    })
                    self.diagnostics.source_results.append(
                        SourceResult(
                            source_id=source.source_id,
                            success=False,
                            items_collected=0,
                            items_normalized=0,
                            error=f"future_timeout_after_{per_future_timeout}s",
                            latency_ms=per_future_timeout * 1000,
                        )
                    )
                except Exception as e:
                    self.diagnostics.sources_failed += 1
                    self.diagnostics.cache_misses += 1
                    self.diagnostics.errors.append({
                        "source_id": source.source_id,
                        "error": f"unexpected: {type(e).__name__}: {str(e)[:200]}",
                    })

        self.diagnostics.finished_at = datetime.utcnow().isoformat()
        self.diagnostics.total_latency_ms = (time.perf_counter() - total_start) * 1000
        logger.info(
            "[DISCOVERY_TIMING] stage=collector_collect ms=%.1f attempted=%s cache_hits=%s cache_misses=%s",
            self.diagnostics.total_latency_ms,
            self.diagnostics.sources_attempted,
            self.diagnostics.cache_hits,
            self.diagnostics.cache_misses,
        )
        return all_items, self.diagnostics

    def get_diagnostics(self) -> CollectorDiagnostics:
        """Get current diagnostics."""
        return self.diagnostics


def collect_sources(
    sources: list[SourceMetadata],
    max_workers: int = DEFAULT_MAX_WORKERS,
    per_feed_limit: int = 30,
) -> tuple[list[RawNewsItem], CollectorDiagnostics]:
    """Convenience function to collect from sources.

    Returns:
        Tuple of (all_items, diagnostics)
    """
    collector = CollectorV2(
        max_workers=max_workers,
        per_feed_limit=per_feed_limit,
    )
    return collector.collect(sources)
