"""Event Clusterer - groups reports describing the same real-world event.

Key principle: The primary newsroom unit is a REAL-WORLD EVENT, not an article URL.

Multiple reports (CoinDesk, The Block, official source) → ONE NewsEvent.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from rapidfuzz.fuzz import token_set_ratio

from .normalizer import normalize_title
from .raw_news_item import RawNewsItem


@dataclass
class ClusterConfig:
    """Configuration for event clustering.

    Attributes:
        headline_similarity_threshold: Minimum headline similarity (0-100)
        entity_overlap_required: Require at least one shared entity
        max_time_gap_hours: Maximum time gap between reports
        keyword_overlap_min: Minimum shared keywords
    """
    headline_similarity_threshold: int = 72
    entity_overlap_required: bool = True
    max_time_gap_hours: float = 36.0
    keyword_overlap_min: int = 2


@dataclass
class EventReport:
    """A report linked to a news event."""
    report_id: str
    source: str
    source_id: str
    source_authority: float
    headline: str
    url: str
    published_at: str | None
    retrieved_at: str
    description: str
    entities: list[str]
    raw_item_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_raw_item(cls, item: RawNewsItem) -> EventReport:
        return cls(
            report_id=item.id,
            source=item.source,
            source_id=item.source_id,
            source_authority=item.source_authority,
            headline=item.headline,
            url=item.canonical_url,
            published_at=item.published_at,
            retrieved_at=item.retrieved_at,
            description=item.description,
            entities=item.entities,
            raw_item_id=item.id,
        )


@dataclass
class Development:
    """A meaningful development in an event's timeline."""
    timestamp: str
    description: str
    source: str
    source_id: str
    reason: str
    report_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NewsEvent:
    """A real-world news event with multiple source reports.

    Attributes:
        event_id: Unique event identifier
        canonical_title: Representative headline
        topic: Primary topic/category
        entities: Key entities involved
        first_seen: When first report was received
        last_seen: When latest report was received
        reports: All linked reports
        sources: Unique source names
        source_ids: Unique source IDs
        developments: Timeline of meaningful developments
        momentum_history: Score history over time
        state: Current event state
        followed: Whether event is being followed
        ignored: Whether event is ignored
        publication_status: Current publication status
    """
    event_id: str = field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:8]}")
    canonical_title: str = ""
    topic: str = ""
    entities: frozenset[str] = field(default_factory=frozenset)
    first_seen: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    last_seen: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    reports: list[EventReport] = field(default_factory=list)
    momentum: str = "LOW"  # LOW, STABLE, RISING, HIGH
    momentum_score: float = 0.0
    developments: list[Development] = field(default_factory=list)
    state: str = "DISCOVERED"
    followed: bool = False
    ignored: bool = False
    novel: bool = True
    breaking_signal: bool = False

    @property
    def sources(self) -> list[str]:
        return sorted({r.source for r in self.reports})

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def primary_sources(self) -> list[str]:
        """Return sources with higher authority (regulators, official)."""
        return sorted({r.source for r in self.reports if r.source_authority >= 0.9})

    @property
    def has_primary_evidence(self) -> bool:
        """Check if event has primary evidence sources."""
        return any(r.source_authority >= 0.9 for r in self.reports)

    @property
    def age_hours(self) -> float:
        """Calculate age of event in hours."""
        try:
            first = datetime.fromisoformat(self.first_seen.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return round((now - first).total_seconds() / 3600, 2)
        except Exception:
            return 0.0

    @property
    def coverage_velocity(self) -> float:
        """Reports per hour since first seen.

        Minimum observation window of 1 hour to avoid spurious
        high velocity from single reports.
        """
        MINIMUM_OBSERVATION_HOURS = 1.0

        age = self.age_hours
        if age < MINIMUM_OBSERVATION_HOURS:
            age = MINIMUM_OBSERVATION_HOURS

        return round(len(self.reports) / age, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "canonical_title": self.canonical_title,
            "topic": self.topic,
            "entities": list(self.entities),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "reports": [r.to_dict() for r in self.reports],
            "sources": self.sources,
            "source_count": self.source_count,
            "primary_sources": self.primary_sources,
            "has_primary_evidence": self.has_primary_evidence,
            "momentum": self.momentum,
            "momentum_score": self.momentum_score,
            "developments": [d.to_dict() for d in self.developments],
            "state": self.state,
            "followed": self.followed,
            "ignored": self.ignored,
            "novel": self.novel,
            "breaking_signal": self.breaking_signal,
            "age_hours": self.age_hours,
            "coverage_velocity": self.coverage_velocity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewsEvent:
        """Create from dict (for persistence)."""
        event = cls(
            event_id=data.get("event_id", f"evt-{uuid.uuid4().hex[:8]}"),
            canonical_title=data.get("canonical_title", ""),
            topic=data.get("topic", ""),
            entities=frozenset(data.get("entities", [])),
            first_seen=data.get("first_seen", datetime.utcnow().isoformat()),
            last_seen=data.get("last_seen", datetime.utcnow().isoformat()),
            momentum=data.get("momentum", "LOW"),
            momentum_score=data.get("momentum_score", 0.0),
            state=data.get("state", "DISCOVERED"),
            followed=data.get("followed", False),
            ignored=data.get("ignored", False),
            novel=data.get("novel", True),
            breaking_signal=data.get("breaking_signal", False),
        )
        # Restore reports
        for r_data in data.get("reports", []):
            event.reports.append(EventReport(**r_data))
        # Restore developments
        for d_data in data.get("developments", []):
            event.developments.append(Development(**d_data))
        return event


class EventClusterer:
    """Deterministic event clustering engine.

    Groups reports into real-world events using:
    - Headline similarity
    - Entity overlap
    - Keyword/topic overlap
    - Time proximity
    - Source independence
    """

    def __init__(self, config: ClusterConfig | None = None):
        self.config = config or ClusterConfig()
        self.events: list[NewsEvent] = []

        self.stats = {
            "items_processed": 0,
            "new_events_created": 0,
            "existing_events_updated": 0,
            "merge_attempts": 0,
            "merges": 0,
        }

    def _headline_similarity(self, a: str, b: str) -> int:
        """Calculate headline similarity (0-100)."""
        na = normalize_title(a)
        nb = normalize_title(b)
        if not na or not nb:
            return 0
        return token_set_ratio(na, nb)

    def _time_gap_hours(self, a: str | None, b: str | None) -> float:
        """Calculate time gap in hours."""
        if not a or not b:
            return 0.0  # Assume no gap if timestamps missing

        try:
            dt_a = datetime.fromisoformat(a.replace("Z", "+00:00"))
            dt_b = datetime.fromisoformat(b.replace("Z", "+00:00"))
            return abs((dt_a - dt_b).total_seconds()) / 3600
        except Exception:
            return 0.0  # Assume no gap on parse error

    def _has_entity_overlap(self, item: RawNewsItem, event: NewsEvent) -> bool:
        """Check if item shares MEANINGFUL entities with event.

        Requires at least one significant entity overlap OR
        if both have entities, require minimum 1 shared.
        """
        if not self.config.entity_overlap_required:
            return True

        item_entities = set(item.entities) if item.entities else set()
        event_entities = set(event.entities) if event.entities else set()

        if not event_entities and not item_entities:
            return True  # No entities to match

        if not event_entities or not item_entities:
            return False  # One has entities, other doesn't

        # Find overlap
        overlap = item_entities & event_entities

        # Require at least 1 common entity for clustering
        return len(overlap) >= 1

    def _has_same_org_match(self, item: RawNewsItem, event: NewsEvent) -> bool:
        """Check if item and event share the same primary organization/action.

        This catches same-event-different-wording like:
        'CFTC files...' vs 'CFTC submits...'
        """
        # Look for organization + action pattern
        org_keywords = {'cftc', 'sec', 'fed', 'treasury', 'white house', 'congress',
                       'coinbase', 'binance', 'bitcoin', 'ethereum', 'sec',
                       'facebook', 'meta', 'google', 'apple', 'tesla', 'mica'}

        item_text = (item.headline + ' ' + item.description).lower()
        event_text = (event.canonical_title + ' ' + event.topic).lower()

        # Find org overlap
        item_orgs = {w for w in org_keywords if w in item_text}
        event_orgs = {w for w in org_keywords if w in event_text}

        if not item_orgs or not event_orgs:
            return False

        return len(item_orgs & event_orgs) > 0

    def _is_same_event_paraphrase(self, item: RawNewsItem, event: NewsEvent) -> tuple[bool, float]:
        """Detect if two headlines describe the same event despite wording differences.

        Returns (is_same_event, confidence)
        """
        # Check for same organization + action pattern
        org_action_terms = ['files', 'submits', 'proposes', 'announces', 'launches',
                          'approves', 'rejects', 'files', 'regulation', 'rule', 'act']

        item_lower = item.headline.lower()
        event_lower = event.canonical_title.lower()

        # Find common organizations
        orgs = {'cftc', 'sec', 'white house', 'congress', 'coinbase', 'binance',
                'bitcoin', 'avalanche', 'solana', 'ethereum'}

        item_orgs = {o for o in orgs if o in item_lower}
        event_orgs = {o for o in orgs if o in event_lower}

        # Find common action terms
        item_actions = {a for a in org_action_terms if a in item_lower}
        event_actions = {a: a in event_lower for a in org_action_terms}

        # If same org + similar action within time window
        if item_orgs and event_orgs and (item_orgs & event_orgs):
            # Check for similar action
            if any(a in item_lower for a in ['files', 'submits']) and \
               any(a in event_lower for a in ['files', 'submits']):
                return True, 0.8

        return False, 0.0

    def _has_keyword_overlap(self, item: RawNewsItem, event: NewsEvent) -> bool:
        """Check if item shares minimum keywords with event."""
        if not event.canonical_title:
            return True
        event_keywords = set(normalize_title(event.canonical_title).split())
        item_keywords = set(item.keywords)
        overlap = len(event_keywords & item_keywords)
        return overlap >= self.config.keyword_overlap_min or \
               (len(event_keywords) > 0 and overlap >= len(event_keywords) // 2)

    def _find_match(self, item: RawNewsItem) -> tuple[NewsEvent | None, str]:
        """Find matching event for an item.

        Returns:
            Tuple of (matched_event, reason) or (None, reason)
        """
        self.stats["merge_attempts"] += 1

        best_match: NewsEvent | None = None
        best_score: int = 0
        reason: str = ""

        for event in self.events:
            # Check time proximity
            time_gap = min(
                self._time_gap_hours(item.published_at, event.first_seen),
                self._time_gap_hours(item.published_at, event.last_seen),
            )
            if time_gap > self.config.max_time_gap_hours:
                continue  # Too far apart in time

            # Check for same-event paraphrase first
            is_paraphrase, conf = self._is_same_event_paraphrase(item, event)
            if is_paraphrase and time_gap < 24:  # Same event, close time
                best_match = event
                best_score = int(conf * 100)
                reason = f"paraphrase:org_action:{conf:.2f}"
                break  # Strong match, stop searching

            # Check headline similarity
            sim = self._headline_similarity(item.headline, event.canonical_title)
            if sim < self.config.headline_similarity_threshold:
                continue  # Headlines too different

            # Check entity overlap (required for clustering)
            if not self._has_entity_overlap(item, event):
                continue

            # Check keyword overlap
            if not self._has_keyword_overlap(item, event):
                continue

            # Stronger match than current best?
            if sim > best_score:
                best_score = sim
                best_match = event
                reason = f"sim:{sim}:time:{time_gap:.1f}h"

        return best_match, reason

    def _is_meaningful_development(self, item: RawNewsItem, event: NewsEvent) -> tuple[bool, str]:
        """Check if an item represents a meaningful development.

        Heuristics:
        - New official/regulator source
        - Significantly different headline with same entities
        - Time gap suggesting new development
        """
        # New primary source
        if item.source_authority >= 0.9:
            existing_primary = {r.source_id for r in event.reports if r.source_authority >= 0.9}
            if item.source_id not in existing_primary:
                return True, "new_primary_source"

        # Different source + significant time gap
        existing_sources = {r.source_id for r in event.reports}
        if item.source_id not in existing_sources:
            time_gap = self._time_gap_hours(item.published_at, event.last_seen)
            if time_gap > 6:  # 6 hours later from new source
                return True, "new_source+time_gap"

        # Headline significantly different but similar entities
        sim = self._headline_similarity(item.headline, event.canonical_title)
        if sim < 85 and len(event.reports) >= 1:
            # Check for new information
            if len(set(item.entities) & event.entities) >= max(1, len(event.entities) // 2):
                return True, "significant_headline_change"

        return False, "repetition"

    def process_item(self, item: RawNewsItem) -> NewsEvent:
        """Process a single item.

        Returns:
            The Event (new or existing) this item belongs to
        """
        self.stats["items_processed"] += 1

        # Try to find matching event
        matched_event, reason = self._find_match(item)

        if matched_event:
            # Add to existing event
            report = EventReport.from_raw_item(item)
            matched_event.reports.append(report)
            matched_event.last_seen = datetime.utcnow().isoformat()

            # Check for development
            is_development, dev_reason = self._is_meaningful_development(item, matched_event)
            if is_development:
                dev = Development(
                    timestamp=datetime.utcnow().isoformat(),
                    description=f"New report: {item.headline[:80]}...",
                    source=item.source,
                    source_id=item.source_id,
                    reason=dev_reason,
                    report_ids=[r.report_id for r in matched_event.reports[-3:]],  # Recent reports
                )
                matched_event.developments.append(dev)

            self.stats["merges"] += 1
            self.stats["existing_events_updated"] += 1
            return matched_event

        # Create new event
        event = NewsEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            canonical_title=item.headline,
            topic=item.topics[0] if item.topics else "GENERAL",
            entities=frozenset(item.entities),
            first_seen=datetime.utcnow().isoformat(),
            last_seen=datetime.utcnow().isoformat(),
            state="DISCOVERED",
        )
        report = EventReport.from_raw_item(item)
        event.reports.append(report)

        self.events.append(event)
        self.stats["new_events_created"] += 1
        return event

    def cluster_batch(self, items: list[RawNewsItem]) -> list[NewsEvent]:
        """Process a batch of items into clustered events.

        Returns:
            List of NewsEvents (newly created or updated)
        """
        affected_events: set[str] = set()

        for item in items:
            event = self.process_item(item)
            affected_events.add(event.event_id)

        return [e for e in self.events if e.event_id in affected_events]

    def get_events(self) -> list[NewsEvent]:
        """Get all events."""
        return list(self.events)

    def get_event(self, event_id: str) -> NewsEvent | None:
        """Get a specific event by ID."""
        for event in self.events:
            if event.event_id == event_id:
                return event
        return None

    def get_followed_events(self) -> list[NewsEvent]:
        """Get all followed events."""
        return [e for e in self.events if e.followed]

    def get_ignored_events(self) -> list[NewsEvent]:
        """Get all ignored events."""
        return [e for e in self.events if e.ignored]

    def get_active_events(self) -> list[NewsEvent]:
        """Get all active (not ignored) events."""
        return [e for e in self.events if not e.ignored]

    def mark_followed(self, event_id: str, followed: bool = True) -> NewsEvent | None:
        """Mark an event as followed/unfollowed."""
        event = self.get_event(event_id)
        if event:
            event.followed = followed
            if followed:
                event.ignored = False
        return event

    def mark_ignored(self, event_id: str, ignored: bool = True) -> NewsEvent | None:
        """Mark an event as ignored/unignored."""
        event = self.get_event(event_id)
        if event:
            event.ignored = ignored
            if ignored:
                event.followed = False
        return event

    def get_stats(self) -> dict[str, Any]:
        """Get clustering statistics."""
        return {
            **self.stats,
            "total_events": len(self.events),
            "followed_events": len([e for e in self.events if e.followed]),
            "ignored_events": len([e for e in self.events if e.ignored]),
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.stats = {
            "items_processed": 0,
            "new_events_created": 0,
            "existing_events_updated": 0,
            "merge_attempts": 0,
            "merges": 0,
        }
