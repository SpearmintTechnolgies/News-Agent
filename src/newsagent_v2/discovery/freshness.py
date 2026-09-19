"""Freshness Engine - filters stale and invalid reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .raw_news_item import RawNewsItem


@dataclass
class FreshnessConfig:
    """Configuration for freshness filtering.

    Attributes:
        max_age_hours: Maximum age to accept (None = no limit)
        require_timestamp: Reject items without valid timestamp
        seen_urls: Set of URLs already processed (for duplicate URL detection)
        seen_fingerprints: Set of fingerprints already processed
    """
    max_age_hours: float | None = 48.0
    require_timestamp: bool = False  # Allow items without timestamps
    seen_urls: set[str] | None = None
    seen_fingerprints: set[str] | None = None

    def __post_init__(self):
        if self.seen_urls is None:
            self.seen_urls = set()
        if self.seen_fingerprints is None:
            self.seen_fingerprints = set()


class FreshnessResult:
    """Result of freshness check."""

    def __init__(self, accepted: bool, reason: str | None = None):
        self.accepted = accepted
        self.reason = reason

    def __bool__(self) -> bool:
        return self.accepted


class FreshnessEngine:
    """Filters news items based on freshness criteria.

    Rejects:
    - Stale stories outside configured freshness policy
    - Invalid/unusable timestamps (when require_timestamp=True)
    - Identical URLs already processed
    - Identical fingerprints already processed
    """

    def __init__(self, config: FreshnessConfig | None = None):
        self.config = config or FreshnessConfig()
        self.stats = {
            "checked": 0,
            "accepted": 0,
            "rejected": 0,
            "rejected_too_old": 0,
            "rejected_no_timestamp": 0,
            "rejected_duplicate_url": 0,
            "rejected_duplicate_content": 0,
        }

    def check(self, item: RawNewsItem, now: datetime | None = None) -> FreshnessResult:
        """Check if an item passes freshness criteria."""
        self.stats["checked"] += 1

        # Check duplicate URL
        if item.canonical_url in (self.config.seen_urls or set()):
            self.stats["rejected"] += 1
            self.stats["rejected_duplicate_url"] += 1
            return FreshnessResult(False, "duplicate_url")

        # Check duplicate content fingerprint
        if item.fingerprint in (self.config.seen_fingerprints or set()):
            self.stats["rejected"] += 1
            self.stats["rejected_duplicate_content"] += 1
            return FreshnessResult(False, "duplicate_content")

        # Check timestamp requirements
        age = item.age_hours(now)

        if age is None:
            if self.config.require_timestamp:
                self.stats["rejected"] += 1
                self.stats["rejected_no_timestamp"] += 1
                return FreshnessResult(False, "no_timestamp")
            # No timestamp but not required
            self.stats["accepted"] += 1
            return FreshnessResult(True)

        # Check max age
        if self.config.max_age_hours is not None:
            if age > self.config.max_age_hours:
                self.stats["rejected"] += 1
                self.stats["rejected_too_old"] += 1
                return FreshnessResult(False, f"too_old:{age:.1f}h")

        self.stats["accepted"] += 1
        return FreshnessResult(True)

    def filter_batch(self, items: list[RawNewsItem], now: datetime | None = None) -> tuple[list[RawNewsItem], list[tuple[RawNewsItem, str]]]:
        """Filter a batch of items.

        Returns:
            Tuple of (accepted_items, rejected_items_with_reasons)
        """
        accepted: list[RawNewsItem] = []
        rejected: list[tuple[RawNewsItem, str]] = []

        for item in items:
            result = self.check(item, now)
            if result:
                accepted.append(item)
            else:
                rejected.append((item, result.reason or "unknown"))

        return accepted, rejected

    def mark_processed(self, item: RawNewsItem) -> None:
        """Mark an item as processed for duplicate detection."""
        if self.config.seen_urls is not None and item.canonical_url:
            self.config.seen_urls.add(item.canonical_url)
        if self.config.seen_fingerprints is not None and item.fingerprint:
            self.config.seen_fingerprints.add(item.fingerprint)

    def get_stats(self) -> dict[str, Any]:
        """Get freshness check statistics."""
        return dict(self.stats)

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        for key in self.stats:
            self.stats[key] = 0
