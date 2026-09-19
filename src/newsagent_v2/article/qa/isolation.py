"""Deterministic cross-story isolation. No LLM."""

from __future__ import annotations

import re
from typing import Any

from newsagent_v2.article.input import evidence_text_blobs
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue
from newsagent_v2.article.qa.textutil import number_tokens

GENERIC_NUMBERS = frozenset({"1", "2", "3", "5", "10", "20", "100", "2024", "2025", "2026"})


def distinctive_numbers(text: str) -> set[str]:
    found: set[str] = set()
    for token in number_tokens(text):
        digits = re.sub(r"[^\d]", "", token)
        if token in GENERIC_NUMBERS or digits in GENERIC_NUMBERS:
            continue
        if "$" in token or "£" in token or "€" in token or len(digits) >= 4:
            found.add(token)
            if digits:
                found.add(digits)
    return found


def check_cross_story_contamination(
    article: dict[str, Any],
    article_input: dict[str, Any],
    other_article_inputs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not other_article_inputs:
        return issues
    body = " ".join(
        str(article.get(key) or "")
        for key in ("headline", "dek", "article_body")
    )
    own_text = " ".join(evidence_text_blobs(article_input))
    own_nums = distinctive_numbers(own_text)
    own_id = str(article_input.get("event_id") or article.get("event_id") or "")
    for other in other_article_inputs:
        if not isinstance(other, dict):
            continue
        other_id = str(other.get("event_id") or "")
        if other_id and other_id == own_id:
            continue
        if other_id and other_id in body:
            issues.append(
                issue(
                    code="cross_story_contamination",
                    message=f"article mentions another event_id {other_id}",
                    severity=SEVERITY_CRITICAL,
                    module="isolation",
                )
            )
        other_nums = distinctive_numbers(" ".join(evidence_text_blobs(other))) - own_nums
        hits = [token for token in other_nums if token and token in body]
        if hits:
            issues.append(
                issue(
                    code="cross_story_contamination",
                    message=(
                        f"article uses distinctive facts from event {other_id or 'other'} "
                        "that are not in this story's evidence"
                    ),
                    severity=SEVERITY_CRITICAL,
                    module="isolation",
                )
            )
            break
    return issues
