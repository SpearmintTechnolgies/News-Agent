"""Stories already offered by /make, so the next run continues past them.

Event ids are new on every scan, so a story is recognized by its normalized
headline or by a shared article URL. Records expire after two days, which
covers the freshness window of a story that was offered early.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.control.sites import COIN_NETWORK_SITE_ID
from newsagent_v2.discovery.normalizer import canonicalize_url, normalize_title


OFFER_BATCH = 10
OFFER_TTL_HOURS = 48
DEFAULT_PATH = Path("data/v5_state/offered_stories.json")


def story_marker(event: Any) -> dict[str, Any]:
    title = normalize_title(getattr(event, "canonical_title", "") or "")
    urls: list[str] = []
    for report in getattr(event, "reports", []) or []:
        url = canonicalize_url(getattr(report, "url", "") or "")
        if url:
            urls.append(url)
    return {"title": title, "urls": sorted(set(urls))}


def _site_id(site_id: str | None) -> str:
    if site_id is not None:
        return site_id
    return str(os.environ.get("NEWSAGENT_ACTIVE_SITE_ID") or "")


def _same_site(record: dict[str, Any], site_id: str) -> bool:
    """Records written before sites were split belong only to Coin Network."""
    if "site_id" not in record:
        return site_id == COIN_NETWORK_SITE_ID
    return str(record.get("site_id") or "") == site_id


def _same_story(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_title = str(left.get("title") or "")
    right_title = str(right.get("title") or "")
    if left_title and left_title == right_title:
        return True
    left_urls = set(left.get("urls") or [])
    right_urls = set(right.get("urls") or [])
    return bool(left_urls and right_urls and (left_urls & right_urls))


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class OfferedStories:
    def __init__(self, records: list[dict[str, Any]] | None = None, *, path: Path | None = None):
        self.records = list(records or [])
        self.path = path or DEFAULT_PATH

    @classmethod
    def load(cls, path: Path | None = None) -> OfferedStories:
        store = path or DEFAULT_PATH
        if not store.exists():
            return cls([], path=store)
        try:
            payload = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls([], path=store)
        records = payload.get("stories") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            records = []
        return cls(records, path=store)

    def already(self, event: Any, *, site_id: str | None = None, now: datetime | None = None) -> bool:
        marker = story_marker(event)
        if not marker["title"] and not marker["urls"]:
            return False
        current = now or datetime.now(timezone.utc)
        wanted = _site_id(site_id)
        for record in self._fresh(current):
            if not _same_site(record, wanted):
                continue
            if _same_story(marker, record):
                return True
        return False

    def remember(self, events: list[Any], *, site_id: str | None = None, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        wanted = _site_id(site_id)
        fresh = self._fresh(current)
        for event in events:
            marker = story_marker(event)
            if not marker["title"] and not marker["urls"]:
                continue
            fresh = [row for row in fresh if not (_same_site(row, wanted) and _same_story(marker, row))]
            fresh.append({
                "title": marker["title"],
                "urls": marker["urls"],
                "offered_at": current.isoformat(),
                "site_id": wanted,
            })
        self.records = fresh
        self._save()

    def _fresh(self, now: datetime) -> list[dict[str, Any]]:
        cutoff = now - timedelta(hours=OFFER_TTL_HOURS)
        kept = []
        for record in self.records:
            offered_at = _parse_time(str(record.get("offered_at") or ""))
            if offered_at is not None and offered_at >= cutoff:
                kept.append(record)
        return kept

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stories": self.records}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)
