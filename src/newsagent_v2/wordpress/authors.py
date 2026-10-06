"""WordPress bylines the bot can assign to a draft.

The site's users are the only names offered. A choice saved with /author is the
byline for the next drafts. A choice on a review card overrides that for one story.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from newsagent_v2.wordpress.config import WordPressConfig

Transport = Callable[..., Any]
DEFAULT_PATH = Path("data/v5_state/author.json")
BYLINE_ROLES = frozenset({"author", "editor", "administrator", "contributor"})
MAX_AUTHORS = 24
# Set when a website is chosen so its bylines stay separate from the other sites.
active_site_id = ""


@dataclass(frozen=True)
class SiteAuthor:
    user_id: int
    name: str


def _authors_from_rows(payload: list[Any]) -> list[SiteAuthor]:
    authors: list[SiteAuthor] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        roles = {str(role) for role in (row.get("roles") or [])}
        if roles and not (roles & BYLINE_ROLES):
            continue
        try:
            user_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(row.get("name") or row.get("slug") or "").strip()
        if user_id <= 0 or not name:
            continue
        authors.append(SiteAuthor(user_id=user_id, name=name))
        if len(authors) >= MAX_AUTHORS:
            break
    return authors


def list_site_authors(config: WordPressConfig, transport: Transport) -> list[SiteAuthor]:
    """Users who can be a byline. Subscribers and nameless accounts are left out.

    Some sites refuse users?context=edit&who=authors. The public user list is the fallback.
    """
    url = urljoin(config.base_url + "/", "wp-json/wp/v2/users?context=edit&who=authors&per_page=100")
    response = transport("GET", url, auth=(config.username, config.app_password))
    payload = response.get("payload") if response.get("ok") else None
    authors = _authors_from_rows(payload) if isinstance(payload, list) else []
    if authors:
        return authors
    public_url = urljoin(config.base_url + "/", "wp-json/wp/v2/users?per_page=100")
    public = transport("GET", public_url)
    public_payload = public.get("payload") if public.get("ok") else None
    if not isinstance(public_payload, list):
        return []
    return _authors_from_rows(public_payload)


def store_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    if active_site_id:
        return DEFAULT_PATH.with_name(f"author-{active_site_id}.json")
    return DEFAULT_PATH


def _read(path: Path | None = None) -> dict[str, Any]:
    store = store_path(path)
    if not store.exists():
        return {}
    try:
        payload = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(payload: dict[str, Any], path: Path | None = None) -> None:
    store = store_path(path)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _author_from(row: Any) -> SiteAuthor | None:
    if not isinstance(row, dict):
        return None
    try:
        user_id = int(row.get("id"))
    except (TypeError, ValueError):
        return None
    name = str(row.get("name") or "").strip()
    if user_id <= 0 or not name:
        return None
    return SiteAuthor(user_id=user_id, name=name)


def remember_default(author: SiteAuthor, path: Path | None = None) -> None:
    payload = _read(path)
    payload["default"] = {"id": author.user_id, "name": author.name}
    _write(payload, path)


def remember_event(event_id: str, author: SiteAuthor, path: Path | None = None) -> None:
    payload = _read(path)
    events = payload.get("events")
    if not isinstance(events, dict):
        events = {}
    events[event_id] = {"id": author.user_id, "name": author.name}
    payload["events"] = events
    _write(payload, path)


def author_for(event_id: str = "", path: Path | None = None) -> SiteAuthor | None:
    """The story's byline, or the default chosen with /author."""
    payload = _read(path)
    events = payload.get("events")
    if event_id and isinstance(events, dict):
        chosen = _author_from(events.get(event_id))
        if chosen is not None:
            return chosen
    return _author_from(payload.get("default"))


def author_id_for(event_id: str = "", path: Path | None = None) -> int | None:
    chosen = author_for(event_id, path)
    return chosen.user_id if chosen else None


def label_for(event_id: str = "", path: Path | None = None) -> str:
    chosen = author_for(event_id, path)
    return chosen.name if chosen else "not chosen"


def author_keyboard(authors: list[SiteAuthor], *, callback_prefix: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": author.name[:40], "callback_data": f"{callback_prefix}:{author.user_id}"}]
            for author in authors
        ]
    }


def find_author(authors: list[SiteAuthor], user_id: str) -> SiteAuthor | None:
    try:
        wanted = int(user_id)
    except (TypeError, ValueError):
        return None
    for author in authors:
        if author.user_id == wanted:
            return author
    return None
