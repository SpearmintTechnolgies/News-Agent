"""Bounded, surgical recovery for post-generation QA failures.

Recovery changes the failed article in place and reruns the existing QA gate.
It never regenerates the story, image, metadata, or successful sections.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.textutil import split_sentences
from newsagent_v2.article.writer.v4.sanitize import sanitize_editorial_artifacts
from newsagent_v2.article.writer.controlled.failures import (
    EDITORIAL_CLEANLINESS_FAILED,
    GROUNDING_FAILED,
    MECHANICS_FAILED,
    QUOTE_GROUNDING_FAILED,
    classify_qa_failure,
)

MAX_RECOVERY_CYCLES = 2
USER_FAILURE_MESSAGE = (
    "Article could not be completed reliably after automatic verification. Please retry."
)
RECOVERABLE_FAILURES = frozenset(
    {
        MECHANICS_FAILED,
        EDITORIAL_CLEANLINESS_FAILED,
        GROUNDING_FAILED,
        QUOTE_GROUNDING_FAILED,
    }
)


@dataclass
class RecoveryOutcome:
    article: dict[str, Any]
    article_input: dict[str, Any]
    qa: dict[str, Any]
    succeeded: bool
    history: list[dict[str, Any]] = field(default_factory=list)
    cycles: int = 0


def _critical(qa: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in qa.get("critical_failures", []) if isinstance(item, dict)]


def _affected(qa: dict[str, Any]) -> list[str]:
    values: list[str] = []
    metrics = qa.get("metrics") or {}
    for item in _critical(qa):
        for key in ("claim_id", "section", "sentence", "text"):
            value = item.get(key)
            if value and str(value) not in values:
                values.append(str(value))
    for value in metrics.get("uncovered_assertive_sentences", []) or []:
        if str(value) not in values:
            values.append(str(value))
    return values


def _sanitize_mechanics(article: dict[str, Any]) -> None:
    for key in ("headline", "dek", "article_body"):
        value = article.get(key)
        if isinstance(value, str):
            value = re.sub(r"[ \t]+", " ", value)
            value = re.sub(r"([!?.,;:])\1+", r"\1", value)
            value = re.sub(r"\s+([.,;:!?])", r"\1", value)
            article[key] = value.strip()
    body = str(article.get("article_body") or "")
    seen: set[str] = set()
    kept: list[str] = []
    for sentence in split_sentences(body):
        key = re.sub(r"\s+", " ", sentence.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            kept.append(sentence.strip())
    if kept:
        article["article_body"] = " ".join(kept)


def _remove_affected_sentences(article: dict[str, Any], affected: list[str]) -> None:
    if not affected:
        return
    body = str(article.get("article_body") or "")
    normalized = {re.sub(r"\s+", " ", value.lower()).strip() for value in affected}
    kept = []
    for sentence in split_sentences(body):
        key = re.sub(r"\s+", " ", sentence.lower()).strip()
        if key not in normalized and not any(key in item or item in key for item in normalized):
            kept.append(sentence.strip())
    article["article_body"] = " ".join(kept).strip()


def _merge_evidence(article_input: dict[str, Any], researched: Any) -> list[dict[str, Any]]:
    pack = researched.get("article_input") if isinstance(researched, dict) else None
    if not isinstance(pack, dict) and isinstance(researched, dict):
        pack = researched.get("pack")
    if not isinstance(pack, dict):
        pack = getattr(researched, "pack", None)
    if not isinstance(pack, dict):
        story = getattr(researched, "story", None)
        pack = story.get("article_input") if isinstance(story, dict) else None
    incoming = pack.get("evidence") if isinstance(pack, dict) else []
    current = article_input.setdefault("evidence", [])
    seen = {str(row.get("url")) for row in current if isinstance(row, dict) and row.get("url")}
    added = []
    for row in incoming or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if url and url in seen:
            continue
        current.append(copy.deepcopy(row))
        if url:
            seen.add(url)
        added.append(copy.deepcopy(row))
    return added


def recover_article(
    article: dict[str, Any],
    article_input: dict[str, Any],
    qa: dict[str, Any],
    *,
    qa_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    research_fn: Callable[[dict[str, Any], list[str]], Any] | None = None,
    rebuild_claim_mapping: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    verify_quotes: Callable[[dict[str, Any], dict[str, Any], list[str]], bool] | None = None,
    usage_fn: Callable[[], dict[str, Any]] | None = None,
    max_cycles: int = MAX_RECOVERY_CYCLES,
) -> RecoveryOutcome:
    """Recover only supported QA classes, with a strict cycle limit."""
    working_article = article
    working_input = article_input
    current_qa = qa
    history: list[dict[str, Any]] = []
    run_qa = qa_fn or (lambda current, data: run_article_qa(current, data, skip_copyright_similarity=True))

    for attempt in range(1, min(max_cycles, MAX_RECOVERY_CYCLES) + 1):
        failure = classify_qa_failure(current_qa)
        if not current_qa.get("qa_passed"):
            pass
        elif current_qa.get("qa_passed"):
            return RecoveryOutcome(working_article, working_input, current_qa, True, history, attempt - 1)
        if failure not in RECOVERABLE_FAILURES:
            break
        failure_issues = _critical(current_qa)
        affected = _affected(current_qa)
        action = ""
        evidence_added: list[dict[str, Any]] = []
        if failure == MECHANICS_FAILED:
            _sanitize_mechanics(working_article)
            action = "deterministic mechanics repair"
        elif failure == EDITORIAL_CLEANLINESS_FAILED:
            sanitize_editorial_artifacts(working_article)
            action = "deterministic editorial sanitization"
        elif failure == GROUNDING_FAILED:
            if research_fn is not None:
                evidence_added = _merge_evidence(working_input, research_fn(working_input, affected))
            if rebuild_claim_mapping is not None:
                rebuild_claim_mapping(working_article, working_input)
            _remove_affected_sentences(working_article, affected)
            action = "targeted gap research, evidence mapping rebuild, affected section repair"
        else:
            verified = bool(verify_quotes and verify_quotes(working_article, working_input, affected))
            if not verified:
                _remove_affected_sentences(working_article, affected)
            action = "quote evidence verification and surgical attribution repair"
        current_qa = run_qa(working_article, working_input)
        entry = {
            "failure_type": failure,
            "exact_reason": failure_issues,
            "affected_claim_or_section": affected,
            "recovery_action": action,
            "evidence_sources_added": [row.get("url") or row.get("source") for row in evidence_added],
            "recovery_succeeded": bool(current_qa.get("qa_passed")),
            "attempt_count": attempt,
            "token_api_usage": usage_fn() if usage_fn else {},
        }
        history.append(entry)
        if current_qa.get("qa_passed"):
            return RecoveryOutcome(working_article, working_input, current_qa, True, history, attempt)
    return RecoveryOutcome(working_article, working_input, current_qa, bool(current_qa.get("qa_passed")), history, len(history))
