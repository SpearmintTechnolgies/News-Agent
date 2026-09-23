"""WordPress draft lifecycle persistence.

Stores event_id → wp_post_id mapping and draft state.
Never stores WordPress credentials.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8

DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[3] / "output" / "wp_drafts"


@dataclass
class WordPressDraftRecord:
    """Persisted draft state for an event."""
    event_id: str
    wp_post_id: int
    article_version: str  # e.g., "v1", "v2"
    image_version: str | None  # e.g., "v1", "v2", or None
    status: str  # "draft", "publish", "pending", etc.
    wp_url: str
    created_at: str
    updated_at: str
    wp_modified: str | None  # WP's last modified time
    featured_media_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WordPressDraftRecord:
        return cls(**d)


class WordPressDraftStore:
    """Persistent storage for WordPress draft lifecycle."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DEFAULT_STORE_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _draft_path(self, event_id: str) -> Path:
        """Sanitized path for event draft record."""
        safe_id = "".join(c for c in event_id if c.isalnum() or c in "-_.")
        return self.root / f"{safe_id}.json"

    def save(self, record: WordPressDraftRecord) -> Path:
        """Save or update draft record atomically."""
        with self._lock:
            record.updated_at = datetime.now(timezone.utc).isoformat()
            path = self._draft_path(record.event_id)
            write_json_utf8(path, record.to_dict())
            return path

    def load(self, event_id: str) -> WordPressDraftRecord | None:
        """Load draft record if exists."""
        path = self._draft_path(event_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return WordPressDraftRecord.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def exists(self, event_id: str) -> bool:
        """Check if draft exists for event."""
        return self._draft_path(event_id).is_file()

    def delete(self, event_id: str) -> bool:
        """Delete draft record. Returns True if existed."""
        with self._lock:
            path = self._draft_path(event_id)
            if path.is_file():
                path.unlink()
                return True
            return False

    def list_all(self) -> list[WordPressDraftRecord]:
        """List all draft records."""
        records = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(WordPressDraftRecord.from_dict(data))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return records
