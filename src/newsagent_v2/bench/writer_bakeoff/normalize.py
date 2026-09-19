"""Normalize bake-off provider JSON into CanonicalArticle. No LLM."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.batch_runner import normalize_batch_output
from newsagent_v2.article.expand import expand_provider_article
from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.writer.normalize import normalize_article_first as canonical_normalize_article_first


def normalize_structured_batch(
    parsed: Any,
    *,
    event_id: str,
    story: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalize_batch_output(
        parsed,
        requested_ids=[event_id],
        stories=[story],
    )
    article = (normalized.get("articles_by_id") or {}).get(event_id)
    failure = (normalized.get("failures_by_id") or {}).get(event_id)
    if isinstance(article, dict):
        expand_provider_article(article, story.get("article_input"))
        materialize_article(article)
    return {
        "article": article,
        "failure": failure,
        "normalized_from": "structured_batch",
    }


def normalize_article_first(
    parsed: Any,
    *,
    event_id: str,
    story: dict[str, Any],
) -> dict[str, Any]:
    return canonical_normalize_article_first(parsed, event_id=event_id, story=story)
