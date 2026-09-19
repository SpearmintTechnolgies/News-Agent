"""Deterministic Top-5 event ID selection. Ranking order is preserved."""

from __future__ import annotations

from typing import Any, Sequence

from newsagent_v2.batch.contract import TOP5_COUNT, BatchError
from newsagent_v2.cluster import EventCluster
from newsagent_v2.evidence import build_evidence_pack


def select_top5_event_ids(
    *,
    ranked_clusters: Sequence[EventCluster] | None = None,
    evidence_pack: dict[str, Any] | None = None,
    event_ids: Sequence[str] | None = None,
) -> list[str]:
    if event_ids is not None:
        ids = [str(item).strip() for item in event_ids if str(item).strip()]
    elif evidence_pack is not None:
        stories = evidence_pack.get("stories")
        if not isinstance(stories, list):
            raise BatchError("invalid_evidence_pack", "evidence pack stories must be a list")
        ids = [
            str(row.get("event_id") or "").strip()
            for row in stories
            if isinstance(row, dict)
        ]
        ids = [item for item in ids if item]
    elif ranked_clusters is not None:
        pack = build_evidence_pack(list(ranked_clusters), limit=TOP5_COUNT)
        ids = [str(row["event_id"]) for row in pack["stories"]]
    else:
        raise BatchError("missing_selection", "provide ranked_clusters, evidence_pack, or event_ids")

    unique = len(set(ids))
    if event_ids is not None:
        if not ids:
            raise BatchError("top5_count", "Top-5 batch requires at least one event ID")
        if len(ids) > TOP5_COUNT:
            raise BatchError(
                "top5_count",
                f"Top-5 batch allows at most {TOP5_COUNT} event IDs, got {len(ids)}",
            )
        if unique != len(ids):
            raise BatchError("duplicate_event_id", "Top-5 event IDs must be unique")
        return ids

    if len(ids) != TOP5_COUNT:
        raise BatchError(
            "top5_count",
            f"Top-5 batch requires exactly {TOP5_COUNT} event IDs, got {len(ids)}",
        )
    if unique != TOP5_COUNT:
        raise BatchError("duplicate_event_id", "Top-5 event IDs must be unique")
    return ids
