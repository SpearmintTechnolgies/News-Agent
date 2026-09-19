from __future__ import annotations

import json
from pathlib import Path

import feedparser

from .models import NewsItem
from .normalize import clean_text, clean_url


def load_sources(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [src for src in data["feeds"] if src.get("enabled", True)]


def collect_rss(
    sources: list[dict],
    per_feed_limit: int = 30,
) -> tuple[list[NewsItem], dict]:

    items: list[NewsItem] = []
    source_health: dict[str, dict] = {}

    for src in sources:
        parsed = feedparser.parse(src["url"])
        raw_count = len(parsed.entries)

        accepted = 0

        for entry in parsed.entries[:per_feed_limit]:
            title = clean_text(entry.get("title"))
            url = clean_url(entry.get("link") or "")
            summary = clean_text(
                entry.get("summary") or entry.get("description")
            )
            published = entry.get("published") or entry.get("updated")

            if not title or not url:
                continue

            items.append(
                NewsItem(
                    source=src["name"],
                    title=title,
                    url=url,
                    published=published,
                    summary=summary,
                    source_type=src.get("source_type", "newsroom"),
                    source_role=src.get("role", "discovery"),
                    source_authority=float(src.get("authority", 0.5)),
                )
            )
            accepted += 1

        source_health[src["name"]] = {
            "feed_entries": raw_count,
            "accepted": accepted,
            "bozo": bool(getattr(parsed, "bozo", False)),
            "healthy": accepted > 0,
        }

    return items, source_health

