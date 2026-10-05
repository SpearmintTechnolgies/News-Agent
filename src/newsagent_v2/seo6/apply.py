"""Final SEO pass before a V6 draft goes to WordPress: internal links from the sitemap, then the score."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from newsagent_v2.seo6.linker import plan_and_apply
from newsagent_v2.seo6.score import score_article
from newsagent_v2.seo6.sitemap import DEFAULT_CACHE, HttpGet, SiteIndex, load_site_index

logger = logging.getLogger(__name__)


def prepare_for_site(
    article: dict[str, Any],
    base_url: str,
    *,
    has_featured_image: bool,
    index: SiteIndex | None = None,
    http_get: HttpGet | None = None,
    cache_path: Path = DEFAULT_CACHE,
) -> dict[str, Any]:
    """Return the article with internal links, Read Also list and an ``seo`` report attached.

    Linking is best effort: if the site index cannot be loaded the article is scored without links.
    """
    out = dict(article)
    if index is None:
        try:
            extra = {"http_get": http_get} if http_get else {}
            index = load_site_index(base_url, cache_path=cache_path, **extra)
        except Exception as exc:  # noqa: BLE001 - linking must never stop a draft
            logger.warning("Site index unavailable, drafting without internal links: %s", exc)
    internal = 0
    duplicate = None
    if index is not None and index.posts:
        body, plan = plan_and_apply(out, index)
        out["article_body"] = body
        out["related_links"] = [{"url": r["url"], "title": r["title"]} for r in plan.related]
        out["internal_links_inline"] = list(plan.inline)
        internal = plan.internal_count
        duplicate = plan.possible_duplicate
    else:
        out["related_links"] = []
        out["internal_links_inline"] = []
    score = score_article(
        out,
        base_url=base_url,
        internal_links=internal,
        external_links=len(out.get("sources") or []),
        has_featured_image=has_featured_image,
        index=index,
    )
    out["seo"] = {
        **score.to_dict(),
        "focus_keyword": out.get("focus_keyphrase") or "",
        "internal_links": internal,
        "possible_duplicate": duplicate,
        "site_posts": len(index.posts) if index else 0,
    }
    return out
