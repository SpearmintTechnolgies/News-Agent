"""
Deterministic article-generation input.

Copies one selected event and its already-collected evidence. Does not
browse, fetch full articles, refresh feeds, or invent evidence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from newsagent_v2.benchmark.contract import EVIDENCE_FIELDS
from newsagent_v2.article.contract import ARTICLE_INPUT_SCHEMA_VERSION

ENRICHMENT_FIELDS = (
    "extracted_text",
    "factual_snippets",
    "retrieved_at_utc",
    "extraction_method",
    "research_only",
    "resolved_url",
    "event_id",
)


def _evidence_row(member: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for field in EVIDENCE_FIELDS:
        row[field] = deepcopy(member.get(field))
    for field in ENRICHMENT_FIELDS:
        if field in member:
            row[field] = deepcopy(member.get(field))
    return row


def build_article_input(
    candidate: dict[str, Any],
    judgment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise TypeError("candidate must be a dict")
    event_id = candidate.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("candidate missing event_id")

    members = candidate.get("evidence")
    if not isinstance(members, list):
        raise TypeError("candidate evidence must be a list")
    evidence = [_evidence_row(member) for member in members]

    judgment = judgment if isinstance(judgment, dict) else {}
    if judgment.get("event_id") not in {None, event_id}:
        raise ValueError("judgment event_id does not match candidate")

    speculation = deepcopy(judgment.get("speculation")) if judgment else None

    return {
        "schema_version": ARTICLE_INPUT_SCHEMA_VERSION,
        "purpose": (
            "Frozen article-generation input for one selected event. "
            "Evidence is copied from selector/benchmark output only."
        ),
        "event_id": event_id,
        "representative_title": deepcopy(candidate.get("representative_title")),
        "deterministic_rank": deepcopy(candidate.get("deterministic_rank")),
        "event_score": deepcopy(candidate.get("event_score")),
        "sources": deepcopy(candidate.get("sources")),
        "source_count": deepcopy(candidate.get("source_count")),
        "category": deepcopy(judgment.get("semantic_category")),
        "is_current_event": deepcopy(judgment.get("is_current_event")),
        "is_background_context": deepcopy(judgment.get("is_background_context")),
        "speculation": speculation,
        "newsworthiness_reasoning": deepcopy(
            judgment.get("newsworthiness_reasoning")
        ),
        "confidence": deepcopy(judgment.get("confidence")),
        "evidence": evidence,
        "browse": False,
        "fetch_fulltext": False,
    }


def candidate_by_event_id(
    editorial_input: dict[str, Any],
    event_id: str,
) -> dict[str, Any]:
    candidates = editorial_input.get("candidates")
    if not isinstance(candidates, list):
        raise TypeError("editorial_input candidates must be a list")
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("event_id") == event_id:
            return candidate
    raise ValueError(f"event_id {event_id!r} is not in the frozen editorial input")


def judgment_by_event_id(
    editorial_output: dict[str, Any],
    event_id: str,
) -> dict[str, Any]:
    judgments = editorial_output.get("judgments")
    if not isinstance(judgments, list):
        raise TypeError("editorial_output judgments must be a list")
    for judgment in judgments:
        if isinstance(judgment, dict) and judgment.get("event_id") == event_id:
            return judgment
    raise ValueError(f"event_id {event_id!r} has no frozen editorial judgment")


def evidence_index(article_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in article_input.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if isinstance(url, str) and url:
            index[url] = row
    return index


def evidence_text_blobs(article_input: dict[str, Any]) -> list[str]:
    blobs: list[str] = []
    for row in article_input.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        for key in ("title", "summary", "source", "extracted_text"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                blobs.append(value)
        snippets = row.get("factual_snippets")
        if isinstance(snippets, list):
            for snippet in snippets:
                if isinstance(snippet, str) and snippet.strip():
                    blobs.append(snippet)
    return blobs
