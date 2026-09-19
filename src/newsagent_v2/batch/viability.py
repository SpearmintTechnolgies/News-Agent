"""
Deterministic Top-5 source viability and backfill.

Rank order is preserved. No LLM. Blocked/thin sources are skipped and
the next ranked candidate is researched until five sufficient stories
exist or the bounded scan is exhausted.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from newsagent_v2.article.enrich import (
    STATUS_INSUFFICIENT,
    STATUS_SUFFICIENT,
    FetchFn,
    enrich_story,
)
from newsagent_v2.article.input import build_article_input
from newsagent_v2.batch.contract import TOP5_COUNT
from newsagent_v2.cluster import EventCluster

DEFAULT_CANDIDATE_SCAN_LIMIT = 12
MIN_CANDIDATE_SCAN_LIMIT = 10
MAX_CANDIDATE_SCAN_LIMIT = 15
SCAN_LIMIT_ENV = "NEWSAGENT_V2_CANDIDATE_SCAN_LIMIT"

STATUS_SELECTED = "selected"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATUS_NOT_SCANNED = "not_scanned"
STATUS_SCAN_LIMIT = "skipped_scan_limit"


def resolve_candidate_scan_limit(
    environ: dict[str, str] | None = None,
    *,
    override: int | None = None,
) -> int:
    if override is not None:
        return max(TOP5_COUNT, int(override))
    source = environ if environ is not None else os.environ
    raw = source.get(SCAN_LIMIT_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_CANDIDATE_SCAN_LIMIT
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_CANDIDATE_SCAN_LIMIT
    return max(MIN_CANDIDATE_SCAN_LIMIT, min(MAX_CANDIDATE_SCAN_LIMIT, value))


def cluster_to_story(cluster: EventCluster, *, original_rank: int) -> dict[str, Any]:
    candidate = {
        "event_id": cluster.event_id,
        "representative_title": cluster.representative.title,
        "deterministic_rank": original_rank,
        "event_score": cluster.event_score,
        "sources": cluster.sources,
        "source_count": cluster.source_count,
        "evidence": [item.to_dict() for item in cluster.members],
    }
    return {
        "event_id": cluster.event_id,
        "article_input": build_article_input(candidate),
        "article_url": cluster.representative.url,
        "source_count": cluster.source_count,
        "original_rank": original_rank,
    }


def _rejection_reason(story: dict[str, Any]) -> str:
    sufficiency = story.get("evidence_sufficiency") or {}
    evidence = (story.get("article_input") or {}).get("evidence") or []
    blocked = [
        str(item.get("extraction_method") or "")
        for item in evidence
        if isinstance(item, dict) and item.get("access_blocked")
    ]
    if blocked:
        return blocked[0]
    status = sufficiency.get("status") or STATUS_INSUFFICIENT
    words = sufficiency.get("extracted_evidence_words")
    facts = sufficiency.get("distinct_fact_count")
    return f"{status}: extracted_words={words} distinct_facts={facts}"


def select_viable_stories(
    ranked_clusters: Sequence[EventCluster],
    *,
    target: int = TOP5_COUNT,
    scan_limit: int | None = None,
    fetch: FetchFn | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    ranked = list(ranked_clusters)
    limit = resolve_candidate_scan_limit(override=scan_limit)
    target_n = max(0, min(int(target), TOP5_COUNT))
    records: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    original_top = [cluster.event_id for cluster in ranked[:target_n]]

    for index, cluster in enumerate(ranked, start=1):
        event_id = cluster.event_id
        if len(selected) >= target_n:
            records.append(
                {
                    "event_id": event_id,
                    "original_rank": index,
                    "viability_status": STATUS_NOT_SCANNED,
                    "rejection_reason": None,
                    "backfill": False,
                }
            )
            continue
        if index > limit:
            records.append(
                {
                    "event_id": event_id,
                    "original_rank": index,
                    "viability_status": STATUS_SCAN_LIMIT,
                    "rejection_reason": f"scan_limit_{limit}",
                    "backfill": False,
                }
            )
            continue
        story = enrich_story(
            cluster_to_story(cluster, original_rank=index),
            fetch=fetch,
            now=now,
        )
        story["original_rank"] = index
        sufficiency = story.get("evidence_sufficiency") or {}
        if sufficiency.get("status") == STATUS_SUFFICIENT:
            is_backfill = event_id not in original_top
            story["viability_status"] = STATUS_SELECTED
            story["backfill"] = is_backfill
            selected.append(story)
            records.append(
                {
                    "event_id": event_id,
                    "original_rank": index,
                    "viability_status": STATUS_SELECTED,
                    "rejection_reason": None,
                    "backfill": is_backfill,
                }
            )
            continue
        story["viability_status"] = STATUS_INSUFFICIENT_EVIDENCE
        reason = _rejection_reason(story)
        records.append(
            {
                "event_id": event_id,
                "original_rank": index,
                "viability_status": STATUS_INSUFFICIENT_EVIDENCE,
                "rejection_reason": reason,
                "backfill": False,
            }
        )

    final_ids = [row["event_id"] for row in selected]
    backfilled_ids = [row["event_id"] for row in selected if row.get("backfill")]
    return {
        "scan_limit": limit,
        "target": target_n,
        "ranked_event_ids": [cluster.event_id for cluster in ranked],
        "original_top_ids": original_top,
        "final_selected_ids": final_ids,
        "backfilled_ids": backfilled_ids,
        "candidates": records,
        "stories": selected,
        "selected_count": len(selected),
        "scanned_count": sum(
            1
            for row in records
            if row["viability_status"]
            in {STATUS_SELECTED, STATUS_INSUFFICIENT_EVIDENCE}
        ),
    }
