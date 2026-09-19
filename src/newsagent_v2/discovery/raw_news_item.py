"""RawNewsItem - normalized representation of collected news."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class RawNewsItem:
    """Normalized representation of a collected news item.

    Maintains provenance - never loses original source information.

    Attributes:
        # Identification
        id: Unique identifier for this item
        fingerprint: Content-based fingerprint for deduplication

        # Source Provenance (preserved exactly as collected)
        source: Original source name
        source_id: Source registry ID
        source_type: Type of source (newsroom, regulator, etc.)
        source_role: DISCOVERY, PRIMARY_EVIDENCE, SECONDARY_EVIDENCE

        # Content (normalized for processing)
        headline: Normalized headline
        description: Normalized description/summary

        # URLs (preserved and normalized)
        canonical_url: Clean URL with tracking params removed
        original_url: Original URL as collected

        # Timestamps
        published_at: Original publication timestamp (if available)
        retrieved_at: When this item was collected

        # Enrichment
        entities: Extracted entities
        topics: Identified topics/categories
        keywords: Relevant keywords

        # Raw preservation
        raw_metadata: Original metadata preserved for debugging/provenance
    """
    # Core identification
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    fingerprint: str = ""

    # Source provenance
    source: str = ""
    source_id: str = ""
    source_type: str = ""
    source_role: str = "discovery"
    source_authority: float = 0.5

    # Content
    headline: str = ""
    description: str = ""

    # URLs
    canonical_url: str = ""
    original_url: str = ""

    # Timestamps
    published_at: str | None = None
    retrieved_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Enrichment
    entities: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # Raw preservation
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    # Processing metadata
    raw_title: str = ""  # Original title before normalization
    raw_description: str = ""  # Original description before normalization

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawNewsItem:
        """Deserialize from dict."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def compute_fingerprint(self) -> str:
        """Compute content fingerprint based on normalized headline."""
        from .normalizer import normalize_title
        normalized = normalize_title(self.headline)
        if not normalized:
            normalized = self.canonical_url
        fp = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
        self.fingerprint = fp
        return fp

    def age_hours(self, now: datetime | None = None) -> float | None:
        """Calculate age in hours from publication."""
        from datetime import timezone, datetime as dt_module

        if not self.published_at:
            return None
        try:
            dt = None

            # Try ISO 8601 format first (most common in RSS)
            try:
                # Handle Python 3.11+ and older versions
                if hasattr(dt_module, 'fromisoformat'):
                    dt = dt_module.fromisoformat(self.published_at.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                pass

            # Try RFC 2822 format
            if dt is None:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(self.published_at)
                except (ValueError, TypeError):
                    pass

            if dt is None:
                return None

            # Ensure timezone-aware
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            # Get current time (timezone-aware UTC)
            current = now
            if current is None:
                current = dt_module.now(timezone.utc)
            elif current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)

            # Calculate age
            age = (current - dt.astimezone(timezone.utc)).total_seconds() / 3600
            return max(0.0, age)
        except (TypeError, ValueError, OverflowError) as e:
            return None

    def get_display_text(self) -> str:
        """Get display text combining headline and truncated description."""
        text = self.headline
        if self.description:
            desc = self.description[:200] + "..." if len(self.description) > 200 else self.description
            text = f"{text}\n\n{desc}"
        return text

    def get_source_summary(self) -> dict[str, Any]:
        """Get summary for display/logging."""
        return {
            "source": self.source,
            "headline": self.headline[:80] + "..." if len(self.headline) > 80 else self.headline,
            "url": self.canonical_url,
            "published": self.published_at,
        }
