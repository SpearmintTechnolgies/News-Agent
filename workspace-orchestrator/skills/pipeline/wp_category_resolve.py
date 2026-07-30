#!/usr/bin/env python3
"""Shared WordPress category resolution and coin-aware guard."""
from __future__ import annotations

import re
from typing import Any

GENERIC_TOKENS = frozenset(
    {
        "coin",
        "coins",
        "token",
        "tokens",
        "memecoin",
        "memecoins",
        "meme",
        "crypto",
        "cryptocurrency",
        "news",
        "latest",
        "the",
        "and",
        "for",
        "with",
        "price",
        "market",
        "chain",
        "based",
        "inu",
    }
)

def slug_to_id_map(categories: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in categories:
        if not isinstance(c, dict):
            continue
        slug = str(c.get("slug") or "").strip()
        cid = c.get("id")
        if slug and cid is not None:
            try:
                out[slug] = int(cid)
            except (TypeError, ValueError):
                pass
    return out


def resolve_slugs_to_ids(
    slugs: list[str] | None,
    categories: list[dict],
) -> tuple[list[str], list[int]]:
    """Map slug list to parallel ids; drop unknown slugs."""
    by_slug = slug_to_id_map(categories)
    resolved_slugs: list[str] = []
    resolved_ids: list[int] = []
    for s in slugs or []:
        s = str(s or "").strip()
        if not s or s in resolved_slugs:
            continue
        if s in by_slug:
            resolved_slugs.append(s)
            resolved_ids.append(by_slug[s])
    return resolved_slugs, resolved_ids


def category_names_for_slugs(slugs: list[str], categories: list[dict]) -> list[str]:
    by_slug = {
        str(c.get("slug")): str(c.get("name") or c.get("slug"))
        for c in categories
        if isinstance(c, dict)
    }
    return [by_slug.get(s, s) for s in slugs]


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 2}


def _story_lead_tokens(data: dict) -> set[str]:
    parts: list[str] = []
    for field in (
        "primary_asset",
        "chart_coin",
        "primary_keyword",
        "primary_headline",
        "topic_theme",
    ):
        val = data.get(field)
        if val:
            parts.append(str(val))
    tokens = _tokenize(" ".join(parts))
    return {t for t in tokens if t not in GENERIC_TOKENS}


def _category_index(categories: list[dict]) -> dict[str, dict[str, Any]]:
    """slug -> {id, slug, name, distinctive_tokens}."""
    slug_counts: dict[str, int] = {}
    raw: list[tuple[str, int, str, set[str]]] = []
    for c in categories:
        if not isinstance(c, dict):
            continue
        slug = str(c.get("slug") or "").strip()
        if not slug:
            continue
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(c.get("name") or slug)
        tokens = _tokenize(f"{name} {slug.replace('-', ' ')}")
        distinctive = {t for t in tokens if t not in GENERIC_TOKENS and len(t) >= 3}
        for t in distinctive:
            slug_counts[t] = slug_counts.get(t, 0) + 1
        raw.append((slug, cid, name, distinctive))

    index: dict[str, dict[str, Any]] = {}
    for slug, cid, name, distinctive in raw:
        index[slug] = {
            "id": cid,
            "slug": slug,
            "name": name,
            "distinctive_tokens": {t for t in distinctive if slug_counts.get(t, 0) == 1},
        }
    return index


def apply_coin_category_guard(
    data: dict,
    categories: list[dict],
    *,
    wp_category_slugs: list[str] | None,
    wp_category_ids: list[int] | None,
) -> tuple[list[str], list[int], str | None]:
    """Ensure lead-coin stories get the matching coin category as primary.

    Returns (slugs, ids, correction_note_or_none).
    """
    slugs = list(wp_category_slugs or [])
    ids = list(wp_category_ids or [])
    if not categories:
        return slugs, ids, None

    if not slugs or not ids:
        slugs, ids = resolve_slugs_to_ids(
            slugs or ([data.get("category")] if data.get("category") else []),
            categories,
        )

    lead = _story_lead_tokens(data)
    if not lead:
        return slugs, ids, None

    index = _category_index(categories)
    best_slug: str | None = None
    best_score = 0
    for slug, info in index.items():
        dist = info["distinctive_tokens"]
        if not dist:
            continue
        overlap = lead & dist
        if not overlap:
            continue
        score = len(overlap)
        if score > best_score:
            best_score = score
            best_slug = slug

    if not best_slug:
        return slugs, ids, None

    primary = slugs[0] if slugs else ""
    if primary == best_slug:
        return slugs, ids, None

    new_slugs = [best_slug] + [s for s in slugs if s != best_slug]
    new_ids = [index[best_slug]["id"]] + [
        index[s]["id"] for s in new_slugs[1:] if s in index
    ]
    note = f"coin guard: primary {primary!r} -> {best_slug!r}"
    data["category"] = best_slug
    return new_slugs, new_ids, note
