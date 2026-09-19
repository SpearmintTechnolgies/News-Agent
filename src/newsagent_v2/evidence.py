from __future__ import annotations

from .cluster import EventCluster
from .models import NewsItem


def evidence_item(item: NewsItem) -> dict:
    return {
        "source": item.source,
        "source_type": item.source_type,
        "source_role": item.source_role,
        "source_authority": item.source_authority,
        "title": item.title,
        "url": item.url,
        "published": item.published,
        "summary": item.summary[:1500],
        "deterministic_score": item.score,
    }


def build_evidence_pack(
    clusters: list[EventCluster],
    limit: int = 5,
) -> dict:

    selected = clusters[:limit]
    stories = []

    for cluster in selected:
        primary = [
            evidence_item(item)
            for item in cluster.members
            if item.source_role == "primary_evidence"
        ]

        secondary = [
            evidence_item(item)
            for item in cluster.members
            if item.source_role != "primary_evidence"
        ]

        stories.append(
            {
                "event_id": cluster.event_id,
                "event_score": cluster.event_score,
                "representative": evidence_item(
                    cluster.representative
                ),
                "sources": cluster.sources,
                "source_count": cluster.source_count,
                "has_primary_evidence": bool(primary),
                "primary_evidence": primary,
                "secondary_evidence": secondary,
                "similarity_reason": cluster.similarity_reason,
            }
        )

    return {
        "story_count": len(stories),
        "stories": stories,
    }