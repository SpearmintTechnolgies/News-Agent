"""Collector V2 - bounded-concurrency RSS collector with per-source error isolation.

One broken source MUST NOT kill /make.
"""

from __future__ import annotations

import concurrent.futures
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import feedparser

from .normalizer import NewsNormalizer
from .raw_news_item import RawNewsItem
from .source_registry import SourceMetadata


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


@dataclass
class CollectorDiagnostics:
    """Diagnostics from a collection run."""
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    raw_items_collected: int = 0
    raw_items_normalized: int = 0
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
                }
                for r in self.source_results
            ],
        }


class CollectorV2:
    """Collector V2 with bounded concurrency and error isolation.

    Features:
    - Bounded concurrency (configurable max workers)
    - Per-source timeouts
    - Safe retries with backoff
    - Per-source error isolation
    - Detailed diagnostics
    """

    def __init__(
        self,
        max_workers: int = 4,
        per_feed_limit: int = 30,
        request_timeout: int = 30,
        max_retries: int = 2,
    ):
        self.max_workers = max_workers
        self.per_feed_limit = per_feed_limit
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.normalizer = NewsNormalizer()
        self.diagnostics = CollectorDiagnostics()

    def _fetch_source(
        self,
        source: SourceMetadata,
    ) -> tuple[list[RawNewsItem], SourceResult]:
        """Fetch and normalize items from a single source.

        Returns:
            Tuple of (items, result)
        """
        start_time = time.perf_counter()
        items: list[RawNewsItem] = []

        for attempt in range(self.max_retries + 1):
            try:
                # Parse the feed
                # Note: feedparser.parse doesn't accept timeout directly
                # Socket timeout is set via socket.setdefaulttimeout
                import socket
                old_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(self.request_timeout)
                try:
                    parsed = feedparser.parse(source.url)
                finally:
                    socket.setdefaulttimeout(old_timeout)

                feed_entries = len(parsed.entries)
                accepted = 0

                # Build source metadata for normalization
                source_meta = {
                    "name": source.name,
                    "source_id": source.source_id,
                    "source_type": source.source_type,
                    "role": source.role,
                    "authority": source.authority,
                }

                for entry in parsed.entries[:self.per_feed_limit]:
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
                        # Skip malformed entries
                        continue

                latency_ms = (time.perf_counter() - start_time) * 1000
                result = SourceResult(
                    source_id=source.source_id,
                    success=True,
                    items_collected=feed_entries,
                    items_normalized=accepted,
                    latency_ms=latency_ms,
                    feed_entries=feed_entries,
                )
                return items, result

            except Exception as e:
                if attempt == self.max_retries:
                    latency_ms = (time.perf_counter() - start_time) * 1000
                    result = SourceResult(
                        source_id=source.source_id,
                        success=False,
                        items_collected=0,
                        items_normalized=0,
                        error=f"{type(e).__name__}: {str(e)[:200]}",
                        latency_ms=latency_ms,
                    )
                    return items, result

                # Wait before retry
                time.sleep(0.5 * (attempt + 1))

        # Should never reach here
        latency_ms = (time.perf_counter() - start_time) * 1000
        return items, SourceResult(
            source_id=source.source_id,
            success=False,
            items_collected=0,
            items_normalized=0,
            error="max_retries_exceeded",
            latency_ms=latency_ms,
        )

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

        # Use ThreadPoolExecutor for bounded concurrency
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_source = {
                executor.submit(self._fetch_source, source): source
                for source in sources
            }

            # Collect results as they complete
            for future in as_completed(future_to_source):
                source = future_to_source[future]
                try:
                    items, result = future.result()
                    all_items.extend(items)
                    self.diagnostics.source_results.append(result)

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

                except Exception as e:
                    self.diagnostics.sources_failed += 1
                    self.diagnostics.errors.append({
                        "source_id": source.source_id,
                        "error": f"unexpected: {type(e).__name__}: {str(e)[:200]}",
                    })

        # Update final diagnostics
        self.diagnostics.finished_at = datetime.utcnow().isoformat()
        self.diagnostics.total_latency_ms = (time.perf_counter() - total_start) * 1000

        return all_items, self.diagnostics

    def get_diagnostics(self) -> CollectorDiagnostics:
        """Get current diagnostics."""
        return self.diagnostics


def collect_sources(
    sources: list[SourceMetadata],
    max_workers: int = 4,
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
