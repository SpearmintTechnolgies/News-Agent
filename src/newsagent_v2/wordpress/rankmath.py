"""Rank Math SEO integration via native REST updateMeta (not wp/v2/posts meta).

Aadhi-proven path: POST /wp-json/rankmath/v1/updateMeta after draft create/update.
Never put rank_math_* into /wp/v2/posts meta (PHP fatal / HTTP 500 on site).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

from .config import WordPressConfig
from .seo_metadata import SEOMetadata
from .adapter import sanitize_wp_error

Transport = Callable[..., Any]

# Single tokens that are never acceptable as a focus keyphrase alone.
# "spot" alone was the observed bad repair for draft 14334.
_WEAK_SINGLE_TOKENS = frozenset({
    "spot", "log", "logs", "amid", "nearly", "daily", "largest", "since",
    "says", "said", "announces", "reports", "report", "new", "latest",
    "update", "market", "markets", "price", "prices", "week", "month",
    "year", "today", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "october", "november", "december", "january",
    "february", "march", "april", "june", "july", "august", "september",
    "billion", "million", "inflow", "inflows", "outflow", "outflows",
    "hit", "hits", "rise", "rises", "fall", "falls", "surge", "surges",
})

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall",
    "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "as", "from", "into", "over", "after",
    "before", "between", "their", "its", "this", "that", "these",
    "those", "than", "then", "also", "just", "about", "above",
})


def _normalize_phrase(raw: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    text = text.strip(" ,.;:|-\"'")
    return text


def is_quality_focus_keyphrase(phrase: str) -> bool:
    """True when phrase is usable as Rank Math focus keyword (not garbage)."""
    cleaned = _normalize_phrase(phrase)
    if not cleaned:
        return False
    if len(cleaned) < 3:
        return False
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+-]*", cleaned.lower()) if w]
    if not words:
        return False
    if len(words) == 1:
        token = words[0]
        if token in _WEAK_SINGLE_TOKENS or token in _STOP_WORDS:
            return False
        # Allow high-signal single tokens only when reasonably long (tickers/brands).
        return len(token) >= 5
    # Multi-word: reject if every word is stop/weak
    content = [w for w in words if w not in _STOP_WORDS]
    return len(content) >= 2 or (len(content) == 1 and len(content[0]) >= 5)


def select_focus_keyphrase(
    *,
    keywords: list[str] | tuple[str, ...] | None = None,
    headline: str = "",
    topic: str = "",
    entities: list[str] | tuple[str, ...] | None = None,
    dek: str = "",
    existing: str = "",
) -> str:
    """Deterministic focus keyphrase. No LLM.

    Priority:
    1. Existing quality phrase (already selected)
    2. First quality entry from article.keywords
    3. Multi-word entity present in headline
    4. 2-4 content words from headline (preferred) or dek
    5. Topic if quality
    6. Best-effort multi-word fallback from headline tokens
    """
    if existing and is_quality_focus_keyphrase(existing):
        return _normalize_phrase(existing)

    for raw in keywords or []:
        candidate = _normalize_phrase(str(raw))
        if is_quality_focus_keyphrase(candidate):
            return candidate

    hl = str(headline or "")
    for raw in entities or []:
        candidate = _normalize_phrase(str(raw))
        if len(candidate.split()) >= 2 and candidate.lower() in hl.lower():
            if is_quality_focus_keyphrase(candidate):
                return candidate

    def _from_text(source: str) -> str:
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+-]*", source.lower())
        content = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
        if len(content) >= 2:
            # Prefer skipping a leading weak single if next words form a better phrase.
            start = 0
            if content[0] in _WEAK_SINGLE_TOKENS and len(content) >= 3:
                # Keep weak token if it forms a known compound ("spot bitcoin etfs")
                compound = " ".join(content[:3])
                if is_quality_focus_keyphrase(compound):
                    return compound
                start = 1
            phrase = " ".join(content[start : start + 3])
            if is_quality_focus_keyphrase(phrase):
                return phrase
            if len(content) >= 2:
                return " ".join(content[:3])
        return ""

    for source in (hl, str(dek or "")):
        phrase = _from_text(source)
        if phrase:
            return phrase

    topic_phrase = _normalize_phrase(str(topic or "").replace("_", " "))
    if is_quality_focus_keyphrase(topic_phrase):
        return topic_phrase

    # Last resort: first two alphanumeric headline tokens joined (never a lone weak token).
    tokens = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+-]*", hl.lower()) if len(w) > 2]
    if len(tokens) >= 2:
        return " ".join(tokens[:2])
    if tokens and is_quality_focus_keyphrase(tokens[0]):
        return tokens[0]
    return ""


def build_rankmath_update_payload(
    *,
    post_id: int,
    seo: SEOMetadata,
    permalink_slug: str | None = None,
) -> dict[str, Any]:
    """Build Aadhi-shaped updateMeta body. permalink = slug, not full URL."""
    title = (seo.seo_title or seo.title or "").strip()
    description = (seo.meta_description or "").strip()
    # Prefer OG/Twitter when present; else mirror SEO title/description.
    fb_title = (seo.open_graph_title or title).strip()
    tw_title = (seo.twitter_title or title).strip()
    fb_desc = (seo.open_graph_description or description).strip()
    tw_desc = (seo.twitter_description or description).strip()
    focus = (seo.focus_keyphrase or "").strip()
    slug = (permalink_slug if permalink_slug is not None else seo.slug) or ""
    slug = str(slug).strip().strip("/")

    meta: dict[str, Any] = {
        "rank_math_title": title,
        "rank_math_facebook_title": fb_title,
        "rank_math_twitter_title": tw_title,
        "rank_math_description": description,
        "rank_math_facebook_description": fb_desc,
        "rank_math_twitter_description": tw_desc,
        "rank_math_focus_keyword": focus,
        "permalink": slug,
    }
    return {
        "objectID": int(post_id),
        "objectType": "post",
        "meta": meta,
    }


def rankmath_update_meta_url(base_url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", "wp-json/rankmath/v1/updateMeta")


def apply_rankmath_seo(
    *,
    config: WordPressConfig,
    transport: Transport,
    post_id: int,
    seo: SEOMetadata,
    permalink_slug: str | None = None,
    featured_media_id: int | None = None,
) -> dict[str, Any]:
    """POST Rank Math updateMeta; optionally set media alt_text.

    Returns {ok, error?, error_code?, rank_math_applied, media_alt_applied}.
    Never changes post status. Caller must keep draft as draft.
    """
    secrets = config.secrets()
    payload = build_rankmath_update_payload(
        post_id=post_id,
        seo=seo,
        permalink_slug=permalink_slug,
    )
    url = rankmath_update_meta_url(config.base_url)
    resp = transport(
        "POST",
        url,
        json=payload,
        auth=(config.username, config.app_password),
    )
    if not resp.get("ok"):
        return {
            "ok": False,
            "error": sanitize_wp_error(
                str(resp.get("error") or "Rank Math updateMeta failed"),
                secrets,
            ),
            "error_code": "rank_math_update_failed",
            "rank_math_applied": False,
            "media_alt_applied": False,
            "payload": payload,
        }

    media_alt_applied = False
    alt = (seo.focus_keyphrase or seo.title or "").strip()
    if featured_media_id and alt:
        media_url = urljoin(
            config.base_url.rstrip("/") + "/",
            f"wp-json/wp/v2/media/{int(featured_media_id)}",
        )
        media_resp = transport(
            "POST",
            media_url,
            json={"alt_text": alt},
            auth=(config.username, config.app_password),
        )
        media_alt_applied = bool(media_resp.get("ok"))

    return {
        "ok": True,
        "error": None,
        "error_code": None,
        "rank_math_applied": True,
        "media_alt_applied": media_alt_applied,
        "payload": payload,
    }
