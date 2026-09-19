"""
Deterministic same-event relation graph encoding.

Mirrors explicitly declared same_event_as edges. Does not decide
whether two events are semantically the same. Does not call AI.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _dedupe_preserve_order(items: list[Any]) -> tuple[list[Any], int]:
    seen: list[Any] = []
    dropped = 0
    for item in items:
        if item in seen:
            dropped += 1
            continue
        seen.append(item)
    return seen, dropped


def _empty_stats() -> dict[str, Any]:
    return {
        "normalization_applied": False,
        "mirrored_relation_count": 0,
        "deduplicated_relation_count": 0,
        "normalized_relation_pairs": [],
        "mirrored_relation_pairs": [],
    }


def normalize_same_event_relations(
    output: Any,
) -> tuple[Any, dict[str, Any]]:
    """
    Return (normalized_copy, stats).

    The input object is never mutated.
    Unknown IDs, malformed IDs, and self-relations are left in place
    for the validator; they are not repaired.
    """
    stats = _empty_stats()
    if not isinstance(output, dict):
        return deepcopy(output), stats

    normalized = deepcopy(output)
    judgments = normalized.get("judgments")
    if not isinstance(judgments, list):
        return normalized, stats

    by_id: dict[str, dict[str, Any]] = {}
    for judgment in judgments:
        if not isinstance(judgment, dict):
            continue
        event_id = judgment.get("event_id")
        if isinstance(event_id, str) and event_id and event_id not in by_id:
            by_id[event_id] = judgment

    for judgment in judgments:
        if not isinstance(judgment, dict):
            continue
        rel = judgment.get("same_event_as")
        if not isinstance(rel, list):
            continue
        unique, dropped = _dedupe_preserve_order(rel)
        if dropped:
            stats["deduplicated_relation_count"] += dropped
            judgment["same_event_as"] = unique

    mirrored_pairs: set[tuple[str, str]] = set()
    for judgment in judgments:
        if not isinstance(judgment, dict):
            continue
        left = judgment.get("event_id")
        if not isinstance(left, str) or left not in by_id:
            continue
        rel = judgment.get("same_event_as")
        if not isinstance(rel, list):
            continue
        for right in list(rel):
            if not isinstance(right, str):
                continue
            if right == left:
                continue
            if right not in by_id:
                continue
            other = by_id[right]
            other_rel = other.get("same_event_as")
            if not isinstance(other_rel, list):
                continue
            if left not in other_rel:
                other_rel.append(left)
                stats["mirrored_relation_count"] += 1
                mirrored_pairs.add(tuple(sorted((left, right))))

    resulting_pairs: set[tuple[str, str]] = set()
    for judgment in judgments:
        if not isinstance(judgment, dict):
            continue
        left = judgment.get("event_id")
        rel = judgment.get("same_event_as")
        if not isinstance(left, str) or not isinstance(rel, list):
            continue
        for right in rel:
            if not isinstance(right, str):
                continue
            if right == left or right not in by_id:
                continue
            resulting_pairs.add(tuple(sorted((left, right))))

    stats["mirrored_relation_pairs"] = [list(pair) for pair in sorted(mirrored_pairs)]
    stats["normalized_relation_pairs"] = [
        list(pair) for pair in sorted(resulting_pairs)
    ]
    stats["normalization_applied"] = bool(
        stats["mirrored_relation_count"] or stats["deduplicated_relation_count"]
    )
    return normalized, stats
