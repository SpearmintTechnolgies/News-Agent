"""Persistent Event Store - stores NewsEvents across restarts.

Uses existing persistence mechanism from ApprovalStore pattern.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event_clusterer import NewsEvent, Development, EventReport


@dataclass
class EventRecord:
    """Stored representation of a news event."""
    event_id: str
    data: dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[3] / "output" / "v5_events"


class EventStore:
    """Persistent store for NewsEvents.

    Uses JSON files for persistence.
    Thread-safe for concurrent access.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DEFAULT_STORE_ROOT)
        self._lock = threading.RLock()
        self._cache: dict[str, NewsEvent] = {}
        self._loaded = False

        # Create directory
        self.root.mkdir(parents=True, exist_ok=True)

    def _event_path(self, event_id: str) -> Path:
        """Get path for an event file."""
        # Shard by first 2 chars of ID
        shard = event_id[-4:-2] if len(event_id) >= 4 else "xx"
        shard_dir = self.root / "events" / shard
        shard_dir.mkdir(parents=True, exist_ok=True)
        return shard_dir / f"{event_id}.json"

    def _load_from_disk(self, event_id: str) -> NewsEvent | None:
        """Load a single event from disk."""
        path = self._event_path(event_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return NewsEvent.from_dict(data.get("data", data))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def load_all(self) -> list[NewsEvent]:
        """Load all events from disk into cache."""
        with self._lock:
            if self._loaded:
                return list(self._cache.values())

            events_dir = self.root / "events"
            if not events_dir.is_dir():
                self._loaded = True
                return []

            count = 0
            for json_file in events_dir.rglob("*.json"):
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    event = NewsEvent.from_dict(data.get("data", data))
                    self._cache[event.event_id] = event
                    count += 1
                except Exception:
                    continue

            self._loaded = True
            return list(self._cache.values())

    def get(self, event_id: str) -> NewsEvent | None:
        """Get an event by ID (from cache or disk)."""
        with self._lock:
            if event_id in self._cache:
                return self._cache[event_id]

            event = self._load_from_disk(event_id)
            if event:
                self._cache[event_id] = event
            return event

    def save(self, event: NewsEvent) -> Path:
        """Save an event to disk."""
        with self._lock:
            path = self._event_path(event.event_id)
            data = {
                "event_id": event.event_id,
                "data": event.to_dict(),
                "updated_at": datetime.utcnow().isoformat(),
                "version": getattr(event, "_version", 1),
            }
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            self._cache[event.event_id] = event
            return path

    def save_batch(self, events: list[NewsEvent]) -> list[Path]:
        """Save multiple events."""
        return [self.save(event) for event in events]

    def update_event(
        self,
        event_id: str,
        updates: dict[str, Any],
    ) -> NewsEvent | None:
        """Update specific fields of an event."""
        with self._lock:
            event = self.get(event_id)
            if not event:
                return None

            # Apply updates
            for key, value in updates.items():
                if hasattr(event, key):
                    setattr(event, key, value)

            # Save updated event
            self.save(event)
            return event

    def merge_or_create(self, event: NewsEvent) -> NewsEvent:
        """Merge event with existing or create new.

        If event exists:
        - Merge reports (avoid duplicates)
        - Update metadata
        - Preserve followed/ignored state
        """
        with self._lock:
            existing = self.get(event.event_id)
            if not existing:
                self.save(event)
                return event

            # Merge reports (avoid duplicates by URL)
            existing_urls = {r.url for r in existing.reports}
            for report in event.reports:
                if report.url not in existing_urls:
                    existing.reports.append(report)
                    existing_urls.add(report.url)

            # Update metadata (take new values if provided)
            if event.canonical_title and event.canonical_title != existing.canonical_title:
                existing.canonical_title = event.canonical_title
            existing.last_seen = datetime.utcnow().isoformat()
            if event.developments:
                existing.developments.extend(event.developments)

            # Merge momentum history
            if event.momentum_score > existing.momentum_score:
                existing.momentum_score = event.momentum_score
                existing.momentum = event.momentum

            self.save(existing)
            return existing

    def mark_followed(self, event_id: str, followed: bool = True) -> NewsEvent | None:
        """Mark event as followed."""
        event = self.get(event_id)
        if event:
            event.followed = followed
            if followed:
                event.ignored = False
            self.save(event)
        return event

    def mark_ignored(self, event_id: str, ignored: bool = True) -> NewsEvent | None:
        """Mark event as ignored."""
        event = self.get(event_id)
        if event:
            event.ignored = ignored
            if ignored:
                event.followed = False
            self.save(event)
        return event

    def mark_state(self, event_id: str, state: str) -> NewsEvent | None:
        """Mark event state."""
        event = self.get(event_id)
        if event:
            event.state = state
            self.save(event)
        return event

    def get_all(self) -> list[NewsEvent]:
        """Get all stored events."""
        self.load_all()
        with self._lock:
            return list(self._cache.values())

    def get_followed(self) -> list[NewsEvent]:
        """Get all followed events."""
        return [e for e in self.get_all() if e.followed]

    def get_ignored(self) -> list[NewsEvent]:
        """Get all ignored events."""
        return [e for e in self.get_all() if e.ignored]

    def get_by_state(self, state: str) -> list[NewsEvent]:
        """Get events by state."""
        return [e for e in self.get_all() if e.state == state]

    def get_recent(self, hours: float = 24.0) -> list[NewsEvent]:
        """Get events from last N hours."""
        now = datetime.now(timezone.utc)
        recent = []
        for event in self.get_all():
            try:
                last = datetime.fromisoformat(event.last_seen.replace("Z", "+00:00"))
                if (now - last).total_seconds() / 3600 <= hours:
                    recent.append(event)
            except Exception:
                continue
        return recent

    def count(self) -> int:
        """Get total event count."""
        return len(self.get_all())

    def get_stats(self) -> dict[str, Any]:
        """Get store statistics."""
        events = self.get_all()
        return {
            "total_events": len(events),
            "followed": len([e for e in events if e.followed]),
            "ignored": len([e for e in events if e.ignored]),
            "by_state": self._count_by_state(events),
            "store_path": str(self.root),
        }

    def _count_by_state(self, events: list[NewsEvent]) -> dict[str, int]:
        """Count events by state."""
        counts: dict[str, int] = {}
        for e in events:
            counts[e.state] = counts.get(e.state, 0) + 1
        return counts

    def delete(self, event_id: str) -> bool:
        """Delete an event."""
        with self._lock:
            if event_id in self._cache:
                del self._cache[event_id]

            path = self._event_path(event_id)
            if path.is_file():
                path.unlink()
                return True
            return False

    def clear_all(self) -> None:
        """Clear all events (for testing)."""
        with self._lock:
            self._cache.clear()
            events_dir = self.root / "events"
            if events_dir.is_dir():
                for json_file in events_dir.rglob("*.json"):
                    json_file.unlink()


# Import here to avoid circular imports
from dataclasses import asdict
