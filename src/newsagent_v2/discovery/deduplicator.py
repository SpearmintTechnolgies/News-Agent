"""Deduplication Engine - handles exact and event-level deduplication.

Separates:
- Exact duplicates (same URL or fingerprint)
- Same-event reports (different articles about same real-world event)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rapidfuzz.fuzz import token_set_ratio

from .normalizer import normalize_title
from .raw_news_item import RawNewsItem


@dataclass
class DeduplicationConfig:
    """Configuration for deduplication.

    Attributes:
        url_match_exact: Match exact canonical URLs
        fingerprint_match: Match content fingerprints
        headline_similarity_threshold: Threshold for headline similarity (0-100)
        time_proximity_hours: Maximum time gap for event clustering
        require_entity_overlap: Require shared entities for event match
    """
    url_match_exact: bool = True
    fingerprint_match: bool = True
    headline_similarity_threshold: int = 75
    time_proximity_hours: float = 24.0
    require_entity_overlap: bool = True


class DeduplicationResult:
    """Result of deduplication check."""

    def __init__(
        self,
        is_duplicate: bool,
        duplicate_type: str | None = None,
        matched_with: str | None = None,
        reason: str | None = None,
    ):
        self.is_duplicate = is_duplicate
        self.duplicate_type = duplicate_type  # "exact" or "event"
        self.matched_with = matched_with  # ID of matched item
        self.reason = reason

    def __bool__(self) -> bool:
        return not self.is_duplicate


@dataclass
class SeenItem:
    """Record of seen item for deduplication."""
    item_id: str
    canonical_url: str
    fingerprint: str
    headline: str
    entities: frozenset[str]
    topics: frozenset[str]
    published_at: str | None = None


class DeduplicationEngine:
    """Deterministic deduplication engine.

    Handles two types of duplicates:
    1. Exact duplicates - same URL or fingerprint
    2. Same-event reports - different articles describing same real-world event
    """

    def __init__(self, config: DeduplicationConfig | None = None):
        self.config = config or DeduplicationConfig()
        self._seen_items: list[SeenItem] = []
        self._seen_urls: set[str] = set()
        self._seen_fingerprints: set[str] = set()

        self.stats = {
            "checked": 0,
            "unique": 0,
            "exact_duplicate": 0,
            "event_duplicate": 0,
            "total_after_dedupe": 0,
        }

    def _headline_similarity(self, a: str, b: str) -> int:
        """Calculate headline similarity (0-100)."""
        na = normalize_title(a)
        nb = normalize_title(b)
        if not na or not nb:
            return 0
        return token_set_ratio(na, nb)

    def _time_overlap(
        self,
        a_published: str | None,
        b_published: str | None,
    ) -> bool:
        """Check if items are within time proximity."""
        if a_published is None or b_published is None:
            return True  # Assume overlap if timestamps missing

        try:
            from email.utils import parsedate_to_datetime
            from datetime import timezone

            dt_a = parsedate_to_datetime(a_published)
            dt_b = parsedate_to_datetime(b_published)

            if dt_a.tzinfo is None:
                dt_a = dt_a.replace(tzinfo=timezone.utc)
            if dt_b.tzinfo is None:
                dt_b = dt_b.replace(tzinfo=timezone.utc)

            diff_hours = abs((dt_a - dt_b).total_seconds()) / 3600
            return diff_hours <= self.config.time_proximity_hours
        except Exception:
            return True  # Assume overlap on parse error

    def _entity_overlap(
        self,
        a: frozenset[str],
        b: frozenset[str],
    ) -> bool:
        """Check if entities overlap."""
        if not a or not b:
            return True  # No entities = assume overlap
        return len(a & b) > 0

    def _has_keywords_match(
        self,
        a: RawNewsItem,
        b: SeenItem,
        min_common: int = 3,
    ) -> bool:
        """Check if items share minimum common keywords."""
        a_kw = set(a.keywords)
        b_kw = set(b.headline.lower().split())  # Simple tokenization for seen items
        common = len(a_kw & b_kw)
        return common >= min_common or (len(a_kw) > 0 and common >= len(a_kw) * 0.5)

    def check(self, item: RawNewsItem) -> DeduplicationResult:
        """Check if item is a duplicate.

        Returns:
            DeduplicationResult - is_duplicate=True if duplicate found
        """
        self.stats["checked"] += 1

        # Check exact URL match
        if self.config.url_match_exact and item.canonical_url:
            if item.canonical_url in self._seen_urls:
                self.stats["exact_duplicate"] += 1
                # Find matched item
                for seen in self._seen_items:
                    if seen.canonical_url == item.canonical_url:
                        return DeduplicationResult(
                            True,
                            "exact",
                            seen.item_id,
                            "canonical_url_match",
                        )

        # Check fingerprint match
        if self.config.fingerprint_match and item.fingerprint:
            if item.fingerprint in self._seen_fingerprints:
                self.stats["exact_duplicate"] += 1
                for seen in self._seen_items:
                    if seen.fingerprint == item.fingerprint:
                        return DeduplicationResult(
                            True,
                            "exact",
                            seen.item_id,
                            "fingerprint_match",
                        )

        # Check same-event matches
        for seen in self._seen_items:
            # Time proximity check
            if self._time_overlap(item.published_at, seen.published_at):
                # Headline similarity
                sim = self._headline_similarity(item.headline, seen.headline)
                if sim >= self.config.headline_similarity_threshold:
                    # Entity overlap check
                    if not self.config.require_entity_overlap or \
                       self._entity_overlap(frozenset(item.entities), seen.entities):
                        self.stats["event_duplicate"] += 1
                        return DeduplicationResult(
                            True,
                            "event",
                            seen.item_id,
                            f"headline_sim:{sim}:entities:{bool(set(item.entities) & set(seen.entities))}",
                        )

        # Not a duplicate
        self.stats["unique"] += 1
        self._record_seen(item)
        return DeduplicationResult(False)

    def _record_seen(self, item: RawNewsItem) -> None:
        """Record item as seen."""
        seen = SeenItem(
            item_id=item.id,
            canonical_url=item.canonical_url,
            fingerprint=item.fingerprint,
            headline=item.headline,
            entities=frozenset(item.entities),
            topics=frozenset(item.topics),
            published_at=item.published_at,
        )
        self._seen_items.append(seen)
        if item.canonical_url:
            self._seen_urls.add(item.canonical_url)
        if item.fingerprint:
            self._seen_fingerprints.add(item.fingerprint)

    def dedupe_batch(
        self,
        items: list[RawNewsItem],
    ) -> tuple[list[RawNewsItem], list[tuple[RawNewsItem, DeduplicationResult]]]:
        """Deduplicate a batch of items.

        Returns:
            Tuple of (unique_items, duplicate_items_with_results)
        """
        unique: list[RawNewsItem] = []
        duplicates: list[tuple[RawNewsItem, DeduplicationResult]] = []

        for item in items:
            result = self.check(item)
            if result:
                unique.append(item)
            else:
                duplicates.append((item, result))

        self.stats["total_after_dedupe"] = len(unique)
        return unique, duplicates

    def get_seen_report(self) -> dict[str, Any]:
        """Get report of seen items."""
        return {
            "unique_items_seen": len(self._seen_items),
            "unique_urls": len(self._seen_urls),
            "unique_fingerprints": len(self._seen_fingerprints),
        }

    def get_stats(self) -> dict[str, Any]:
        """Get deduplication statistics."""
        return dict(self.stats)

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.stats = {
            "checked": 0,
            "unique": 0,
            "exact_duplicate": 0,
            "event_duplicate": 0,
            "total_after_dedupe": 0,
        }

    def clear_seen(self) -> None:
        """Clear all seen items (for testing/debugging)."""
        self._seen_items = []
        self._seen_urls = set()
        self._seen_fingerprints = set()
        self.reset_stats()
