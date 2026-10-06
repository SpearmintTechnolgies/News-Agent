"""Internal master index of successfully published stories.

This is a durable publication registry, not a Google or other search-engine
indexing client. Records are written only after WordPress publication succeeds.
"""

from __future__ import annotations

import json
import html
import re
from urllib.parse import urlencode, urlparse
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8

DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[3] / "output" / "master_index"


@dataclass
class MasterIndexRecord:
    event_id: str
    canonical_url: str
    wp_post_id: int | str | None
    article_version: str | None
    image_version: str | None
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    seo_status: Any = None
    published_at: str = ""
    indexing_state: str = "RECORDED"
    title: str | None = None
    modified_at: str | None = None
    topic: str | None = None
    entities: list[str] = field(default_factory=list)
    seo_metadata: dict[str, Any] | None = None
    source: str = "newsagent"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Keep the historical publication schema stable for existing records.
        if self.source == "newsagent":
            for key in ("title", "modified_at", "topic", "entities", "seo_metadata"):
                data.pop(key, None)
        return data


class MasterIndexStore:
    """Atomic, event-keyed persistence for the internal publication index."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or DEFAULT_STORE_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, event_id: str) -> Path:
        safe_id = "".join(char for char in event_id if char.isalnum() or char in "-_.")
        return self.root / f"{safe_id}.json"

    def record_publication(
        self,
        *,
        event_id: str,
        canonical_url: str,
        wp_post_id: int | str | None,
        article_version: str | None,
        image_version: str | None,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        seo_status: Any = None,
        published_at: str | None = None,
    ) -> MasterIndexRecord:
        record = MasterIndexRecord(
            event_id=event_id,
            canonical_url=canonical_url,
            wp_post_id=wp_post_id,
            article_version=article_version,
            image_version=image_version,
            categories=list(categories or []),
            tags=list(tags or []),
            seo_status=seo_status,
            published_at=published_at or datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            write_json_utf8(self._path(event_id), record.to_dict())
        return record

    def load(self, event_id: str) -> MasterIndexRecord | None:
        path = self._path(event_id)
        if not path.is_file():
            return None
        try:
            return MasterIndexRecord(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def remove_publication(self, event_id: str) -> bool:
        """Remove a publication so unpublished URLs cannot be selected internally."""
        with self._lock:
            path = self._path(event_id)
            if not path.is_file():
                return False
            path.unlink()
            return True

    def list_records(self) -> list[MasterIndexRecord]:
        records: list[MasterIndexRecord] = []
        for path in self.root.glob("*.json"):
            if path.name == "sync_state.json":
                continue
            try:
                record = MasterIndexRecord(**json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            records.append(record)
        return records

    def record_wordpress_post(self, post: dict[str, Any]) -> MasterIndexRecord:
        post_id = post.get("id")
        canonical_url = str(post.get("link") or post.get("canonical_url") or "")
        existing = next(
            (
                row for row in self.list_records()
                if str(row.wp_post_id) == str(post_id)
                or (canonical_url and row.canonical_url == canonical_url)
            ),
            None,
        )
        record = MasterIndexRecord(
            event_id=(existing.event_id if existing else str(post.get("event_id") or f"wp-{post_id}")),
            canonical_url=canonical_url,
            wp_post_id=post_id,
            article_version=existing.article_version if existing else None,
            image_version=existing.image_version if existing else None,
            categories=[str(item) for item in post.get("categories", [])],
            tags=[str(item) for item in post.get("tags", [])],
            seo_status=post.get("seo_status") or post.get("seo_metadata"),
            published_at=str(post.get("date_gmt") or post.get("date") or ""),
            indexing_state="SYNCED",
            title=str(post.get("title") or ""),
            modified_at=str(post.get("modified_gmt") or post.get("modified") or ""),
            topic=str(post.get("topic") or post.get("meta", {}).get("topic") or ""),
            entities=[str(item) for item in post.get("entities", [])],
            seo_metadata=post.get("seo_metadata") or post.get("meta") or {},
            source="wordpress",
        )
        with self._lock:
            write_json_utf8(self._path(record.event_id), record.to_dict())
        return record

    def load_sync_state(self) -> dict[str, Any]:
        path = self.root / "sync_state.json"
        if not path.is_file():
            return {"bootstrap_complete": False, "last_sync_at": None, "last_modified_at": None}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            return {"bootstrap_complete": False, "last_sync_at": None, "last_modified_at": None}

    def save_sync_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            write_json_utf8(self.root / "sync_state.json", state)

    def search_wordpress(
        self,
        query: str,
        *,
        entities: list[str] | None = None,
        limit: int = 3,
        host: str = "",
    ) -> list[MasterIndexRecord]:
        terms = {part.lower() for part in query.split() if part.strip()}
        terms.update(str(item).lower() for item in (entities or []) if str(item).strip())
        wanted = host.lower().removeprefix("www.")
        scored: list[tuple[int, MasterIndexRecord]] = []
        for row in self.list_records():
            if row.source != "wordpress":
                continue
            # Published sync only; skip unusable / deactivated-looking rows.
            if str(getattr(row, "indexing_state", "") or "").upper() in {"REMOVED", "UNPUBLISHED", "DEACTIVATED"}:
                continue
            if not (row.canonical_url or "").startswith(("http://", "https://")):
                continue
            if wanted and wanted not in {"example.com", "site.test"}:
                row_host = (urlparse(row.canonical_url).hostname or "").lower().removeprefix("www.")
                if row_host != wanted:
                    continue
            if not (row.title or "").strip():
                continue
            haystack = " ".join([row.title or "", row.topic or "", *row.entities, *row.categories, *row.tags]).lower()
            score = sum(3 if term in (row.title or "").lower() else 1 for term in terms if term in haystack)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1].modified_at or item[1].published_at),)
        return [row for _, row in scored[: max(0, limit)]]


def _rendered(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered") or ""
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or "")).strip())


def sync_wordpress_posts(
    *,
    config: Any,
    transport: Any,
    store: MasterIndexStore,
    per_page: int = 100,
) -> dict[str, Any]:
    """Bootstrap or incrementally sync every published WP post until exhausted."""
    state = store.load_sync_state()
    bootstrap = not bool(state.get("bootstrap_complete"))
    synced = 0
    page = 1
    newest_modified = state.get("last_modified_at")
    while True:
        params: dict[str, Any] = {
            "status": "publish",
            "orderby": "modified",
            "order": "asc",
            "page": page,
            "per_page": per_page,
            "_fields": "id,link,title,date,date_gmt,modified,modified_gmt,categories,tags,meta,yoast_head_json",
        }
        if not bootstrap and state.get("last_modified_at"):
            params["modified_after"] = state["last_modified_at"]
        response = transport(
            "GET",
            config.base_url.rstrip("/") + "/wp-json/wp/v2/posts?" + urlencode(params),
            auth=(config.username, config.app_password),
        )
        if not response.get("ok") and not bootstrap and params.get("modified_after"):
            # Some installations do not expose modified_after.
            bootstrap = True
            params.pop("modified_after", None)
            response = transport(
                "GET",
                config.base_url.rstrip("/") + "/wp-json/wp/v2/posts?" + urlencode(params),
                auth=(config.username, config.app_password),
            )
        if not response.get("ok"):
            store.save_sync_state({**state, "last_sync_at": datetime.now(timezone.utc).isoformat(), "last_error": response.get("error")})
            return {"ok": False, "bootstrap": bootstrap, "synced": synced, "pages": page, "error": response.get("error")}
        posts = response.get("payload") or []
        if not isinstance(posts, list) or not posts:
            break
        for raw in posts:
            if not isinstance(raw, dict) or raw.get("id") is None:
                continue
            normalized = dict(raw)
            normalized["title"] = _rendered(raw.get("title"))
            normalized["categories"] = raw.get("categories") or []
            normalized["tags"] = raw.get("tags") or []
            normalized["seo_metadata"] = {
                **(raw.get("meta") or {}),
                **(raw.get("yoast_head_json") or {}),
            }
            record = store.record_wordpress_post(normalized)
            newest_modified = max(filter(None, [newest_modified, record.modified_at]), default=newest_modified)
            synced += 1
        page += 1
    new_state = {
        "bootstrap_complete": True,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "last_modified_at": newest_modified,
        "last_page": page - 1,
        "posts_synced": synced,
        "mode": "bootstrap" if bootstrap else "incremental",
    }
    store.save_sync_state(new_state)
    return {"ok": True, "bootstrap": bootstrap, "synced": synced, "pages": page - 1, "state": new_state}