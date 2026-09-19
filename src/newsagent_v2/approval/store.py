"""Local approval-state store. Frozen artifacts only; no regeneration."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets

STATE_GENERATED = "GENERATED"
STATE_QA_FAILED = "QA_FAILED"
STATE_IMAGE_FAILED = "IMAGE_FAILED"
STATE_AWAITING_APPROVAL = "AWAITING_APPROVAL"
STATE_APPROVED = "APPROVED"
STATE_REJECTED = "REJECTED"
STATE_PUBLISHING = "PUBLISHING"
STATE_PUBLISHED = "PUBLISHED"
STATE_PUBLISH_FAILED = "PUBLISH_FAILED"

DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[3] / "output" / "approval"


class ApprovalStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DEFAULT_STORE_ROOT)
        self._lock = threading.Lock()

    def batch_dir(self, batch_id: str) -> Path:
        return self.root / batch_id

    def _story_path(self, batch_id: str, event_id: str) -> Path:
        return self.batch_dir(batch_id) / "stories" / f"{event_id}.json"

    def write_batch(self, batch_id: str, payload: dict[str, Any], secrets: tuple[str, ...] = ()) -> Path:
        path = self.batch_dir(batch_id) / "batch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_utf8(path, redact_secrets(payload, secrets))
        return path

    def read_batch(self, batch_id: str) -> dict[str, Any] | None:
        path = self.batch_dir(batch_id) / "batch.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_story(self, batch_id: str, event_id: str, payload: dict[str, Any], secrets: tuple[str, ...] = ()) -> Path:
        path = self._story_path(batch_id, event_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_utf8(path, redact_secrets(payload, secrets))
        return path

    def read_story(self, batch_id: str, event_id: str) -> dict[str, Any] | None:
        path = self._story_path(batch_id, event_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def cas_story_state(
        self,
        batch_id: str,
        event_id: str,
        *,
        expected: str | set[str],
        new_state: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        allowed = {expected} if isinstance(expected, str) else set(expected)
        with self._lock:
            story = self.read_story(batch_id, event_id)
            if story is None:
                return None
            if story.get("state") not in allowed:
                return story
            story["state"] = new_state
            if extra:
                story.update(extra)
            self.write_story(batch_id, event_id, story)
            return story
