"""Each website has its own categories and authors.

/start lists websites. A website opens that site's categories. A category opens
that site's authors. The story is then filed under the category, with that byline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.control.sites import Site, wordpress_config
from newsagent_v2.wordpress.authors import SiteAuthor, remember_default

Transport = Callable[..., Any]
FLOW_PATH = Path("data/v5_state/site_flow.json")
_pending: dict[str, dict[str, Any]] = {}
_catalog: dict[str, list[dict[str, Any]]] = {}


def fetch_categories(site: Site, transport: Transport) -> list[dict[str, Any]]:
    """Every category on the site, including ones with no posts yet."""
    config = wordpress_config(site)
    rows: list[dict[str, Any]] = []
    for page in range(1, 11):
        response = transport(
            "GET",
            f"{config.base_url}/wp-json/wp/v2/categories?per_page=100&page={page}&hide_empty=false",
            auth=(config.username, config.app_password),
        )
        payload = response.get("payload")
        if not response.get("ok") or not isinstance(payload, list) or not payload:
            break
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = " ".join(str(row.get("name") or "").split())
            if not name or name.casefold() == "uncategorized":
                continue
            try:
                category_id = int(row.get("id"))
                parent_id = int(row.get("parent") or 0)
            except (TypeError, ValueError):
                continue
            rows.append({"id": category_id, "name": name, "parent": parent_id})
        if len(payload) < 100:
            break
    return rows


def category_labels(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Show the parent when two categories share a name."""
    by_id = {row["id"]: row["name"] for row in categories}
    counts: dict[str, int] = {}
    for row in categories:
        counts[row["name"]] = counts.get(row["name"], 0) + 1
    labeled = []
    for row in categories:
        label = row["name"]
        parent = by_id.get(row["parent"], "")
        if counts[row["name"]] > 1 and parent:
            label = f"{parent} · {label}"
        labeled.append({"id": row["id"], "name": row["name"], "label": label[:40]})
    labeled.sort(key=lambda item: item["label"].lower())
    return labeled


def category_keyboard(site_id: str, categories: list[dict[str, Any]]) -> dict[str, Any]:
    buttons = [
        {"text": row["label"], "callback_data": f"c:{site_id}:{row['id']}"}
        for row in category_labels(categories)
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    if not rows:
        rows = [[{"text": "No categories", "callback_data": "c:none:0"}]]
    rows.append([{"text": "SITE SETTINGS", "callback_data": f"site:settings:{site_id}"}])
    return {"inline_keyboard": rows}


def category_menu_text(site_name: str, count: int) -> str:
    return (
        f"{site_name}\n\n"
        f"{count} categories on this site.\n"
        "Pick one. The next step is that site's authors."
    )


def remember_category(chat_id: str, site_id: str, category_id: int, name: str) -> None:
    _pending[str(chat_id)] = {
        "site_id": site_id,
        "category_id": int(category_id),
        "category_name": name,
    }


def pending_category(chat_id: str) -> dict[str, Any] | None:
    row = _pending.get(str(chat_id))
    return dict(row) if row else None


def author_keyboard(site_id: str, authors: list[SiteAuthor]) -> dict[str, Any]:
    buttons = [
        {"text": author.name[:40], "callback_data": f"a:{site_id}:{author.user_id}"}
        for author in authors
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def author_menu_text(site_name: str, category_name: str, count: int) -> str:
    return (
        f"{site_name}\n"
        f"Category: {category_name}\n\n"
        f"{count} authors on this site. Pick the byline.\n"
        "The story is filed in this category."
    )


def remember_publication(site_id: str, category_id: int, category_name: str, author: SiteAuthor) -> None:
    """Keep this site's category and byline for the draft that follows."""
    remember_default(author)
    payload = _read()
    payload[site_id] = {
        "category_id": int(category_id),
        "category_name": category_name,
        "author_id": author.user_id,
        "author_name": author.name,
    }
    _write(payload)


def _active_choice() -> dict[str, Any]:
    from newsagent_v2.wordpress import authors

    site_id = str(authors.active_site_id or "").strip()
    if not site_id:
        return {}
    row = _read().get(site_id)
    return row if isinstance(row, dict) else {}


def current_category_name() -> str:
    return str(_active_choice().get("category_name") or "").strip()


def current_category_id() -> int:
    try:
        return int(_active_choice().get("category_id") or 0)
    except (TypeError, ValueError):
        return 0


def forget_site(site_id: str) -> None:
    """Drop the saved category and byline for a website that was removed."""
    payload = _read()
    if site_id not in payload:
        _catalog.pop(site_id, None)
        return
    payload.pop(site_id, None)
    _write(payload)
    _catalog.pop(site_id, None)


def store_categories(site_id: str, categories: list[dict[str, Any]]) -> None:
    _catalog[site_id] = list(categories)


def cached_categories(site_id: str) -> list[dict[str, Any]]:
    return list(_catalog.get(site_id) or [])


def category_name_for(site_id: str, category_id: int, categories: list[dict[str, Any]]) -> str:
    for row in categories:
        if int(row["id"]) == int(category_id):
            return str(row["name"])
    return ""


def _read() -> dict[str, Any]:
    if not FLOW_PATH.is_file():
        return {}
    try:
        payload = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(payload: dict[str, Any]) -> None:
    FLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLOW_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
