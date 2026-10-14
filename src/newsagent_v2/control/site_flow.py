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
EVENT_CATEGORY_PATH = Path("data/v5_state/event_categories.json")
FILL_GAPS_TOTAL = 15
FILL_PREFIX = "fill:"
HIGH_TREND_PREFIX = "high:"
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
            try:
                count = int(row.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            rows.append({"id": category_id, "name": name, "parent": parent_id, "count": max(0, count)})
        if len(payload) < 100:
            break
    return rows


def lacking_categories(
    categories: list[dict[str, Any]],
    *,
    total: int = FILL_GAPS_TOTAL,
) -> list[dict[str, Any]]:
    """Thinnest categories first. One slot per category, up to ``total``."""
    want = max(1, min(int(total), FILL_GAPS_TOTAL))
    ranked = sorted(
        categories,
        key=lambda row: (int(row.get("count") or 0), str(row.get("name") or "").lower()),
    )
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "parent": int(row.get("parent") or 0),
            "count": int(row.get("count") or 0),
        }
        for row in ranked[:want]
        if str(row.get("name") or "").strip()
    ]


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
    from newsagent_v2.control.story_picker import HIGH_TREND_TOTAL

    rows.append([
        {"text": f"HIGH TREND · {HIGH_TREND_TOTAL}", "callback_data": f"{HIGH_TREND_PREFIX}{site_id}"},
        {"text": f"FILL GAPS · {FILL_GAPS_TOTAL}", "callback_data": f"{FILL_PREFIX}{site_id}"},
    ])
    rows.append([{"text": "SITE SETTINGS", "callback_data": f"site:settings:{site_id}"}])
    return {"inline_keyboard": rows}


def category_menu_text(site_name: str, count: int) -> str:
    from newsagent_v2.control.site_niche import active_niche
    from newsagent_v2.control.story_picker import HIGH_TREND_TOTAL

    niche = active_niche()
    niche_note = ""
    if niche.key != "crypto" or "altcoin" in site_name.lower():
        label = niche.label if niche.key != "crypto" else "Altcoins"
        niche_note = f"Niche: {label} — search and cards stay on that beat.\n"

    return (
        f"{site_name}\n\n"
        f"{count} categories on this site.\n"
        f"{niche_note}"
        f"HIGH TREND — top {HIGH_TREND_TOTAL} freshest high-traffic stories for this site's niche + categories.\n"
        f"FILL GAPS — one story for each of the thinnest {FILL_GAPS_TOTAL} categories.\n"
        "Or pick one category.\n"
        "The next step is that site's authors."
    )


def remember_category(chat_id: str, site_id: str, category_id: int, name: str) -> None:
    _pending[str(chat_id)] = {
        "site_id": site_id,
        "category_id": int(category_id),
        "category_name": name,
        "mode": "category",
    }


def remember_fill_gaps(chat_id: str, site_id: str) -> None:
    """Author pick next; then scan thin categories instead of one tap."""
    _pending[str(chat_id)] = {
        "site_id": site_id,
        "mode": "fill_gaps",
        "category_id": 0,
        "category_name": "",
    }


def remember_high_trend(chat_id: str, site_id: str) -> None:
    """Author pick next; then fetch the top high-traffic trend stories."""
    _pending[str(chat_id)] = {
        "site_id": site_id,
        "mode": "high_trend",
        "category_id": 0,
        "category_name": "",
    }


def pending_is_fill_gaps(chat_id: str) -> bool:
    row = _pending.get(str(chat_id)) or {}
    return str(row.get("mode") or "") == "fill_gaps"


def pending_is_high_trend(chat_id: str) -> bool:
    row = _pending.get(str(chat_id)) or {}
    return str(row.get("mode") or "") == "high_trend"


def best_category_for_event(event: Any, categories: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map a story onto the closest WordPress category by term relevance.

    Returns None when nothing matches — do not invent a random category.
    """
    from newsagent_v2.control.story_picker import (
        _blob,
        _contains,
        category_terms_from_name,
        related_terms,
    )

    if not categories:
        return None
    text = _blob(event)
    best: dict[str, Any] | None = None
    best_hits = 0
    for row in categories:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        terms = category_terms_from_name(name) + related_terms(name)
        hits = sum(1 for term in terms if _contains(text, term))
        if hits > best_hits:
            best_hits = hits
            best = row
    if best_hits <= 0:
        return None
    return best


def bind_event_category(
    event_id: str,
    *,
    site_id: str,
    category_id: int,
    category_name: str,
) -> None:
    """Pin the WordPress category for this story card through RUN STORY."""
    event_key = str(event_id or "").strip()
    if not event_key or not category_id:
        return
    payload = _read_event_categories()
    payload[event_key] = {
        "site_id": site_id,
        "category_id": int(category_id),
        "category_name": str(category_name or "").strip(),
    }
    _write_event_categories(payload)


def category_for_event(event_id: str) -> dict[str, Any] | None:
    row = _read_event_categories().get(str(event_id or "").strip())
    if not isinstance(row, dict):
        return None
    try:
        category_id = int(row.get("category_id") or 0)
    except (TypeError, ValueError):
        return None
    if category_id <= 0:
        return None
    return {
        "site_id": str(row.get("site_id") or ""),
        "category_id": category_id,
        "category_name": str(row.get("category_name") or "").strip(),
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
        "The next step is how many stories to fetch."
    )


def fill_gaps_author_menu_text(site_name: str, count: int) -> str:
    return (
        f"{site_name}\n"
        f"FILL GAPS · up to {FILL_GAPS_TOTAL} stories\n\n"
        f"{count} authors on this site. Pick the byline.\n"
        "I will scan which categories have the fewest posts, "
        "then fetch one writeable story for each thin category."
    )


def high_trend_author_menu_text(site_name: str, count: int) -> str:
    from newsagent_v2.control.story_picker import HIGH_TREND_TOTAL

    return (
        f"{site_name}\n"
        f"HIGH TREND · {HIGH_TREND_TOTAL} stories\n\n"
        f"{count} authors on this site. Pick the byline.\n"
        "I will fetch the freshest high-traffic stories "
        "(multi-source momentum + SEO pull) and file each under the closest category."
    )


FETCH_COUNTS = (1, 3, 5, 10, 15)
FETCH_PREFIX = "fetch:"


def clamp_fetch_count(raw: str) -> int | None:
    """One of the buttons. Anything else is refused."""
    try:
        count = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if count not in FETCH_COUNTS:
        return None
    return count


def fetch_count_keyboard() -> dict[str, Any]:
    buttons = [
        {"text": str(count), "callback_data": f"{FETCH_PREFIX}{count}"}
        for count in FETCH_COUNTS
    ]
    return {"inline_keyboard": [buttons[:3], buttons[3:]]}


def fetch_count_text(site_name: str, category_name: str, author_name: str) -> str:
    return (
        f"{site_name}\n"
        f"Category: {category_name}\n"
        f"Byline: {author_name}\n\n"
        "How many stories should I fetch?\n"
        "1 is a single card. 15 is the largest batch."
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


def _read_event_categories() -> dict[str, Any]:
    if not EVENT_CATEGORY_PATH.is_file():
        return {}
    try:
        payload = json.loads(EVENT_CATEGORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_event_categories(payload: dict[str, Any]) -> None:
    EVENT_CATEGORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVENT_CATEGORY_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
