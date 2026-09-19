from __future__ import annotations

from typing import Any

from newsagent_v2.article.contract import ARTICLE_QA_SCHEMA_VERSION

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"


def issue(
    *,
    code: str,
    message: str,
    severity: str,
    module: str,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "severity": severity,
        "module": module,
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def empty_metrics() -> dict[str, Any]:
    return {
        "article_word_count": 0,
        "headline_word_count": 0,
        "claim_count": 0,
        "claims_with_evidence": 0,
        "evidence_coverage": 0.0,
        "quote_count": 0,
        "exact_overlap_count": 0,
        "exact_overlap_ngram_hits": 0,
        "max_similarity": 0.0,
        "repeated_sentence_count": 0,
        "grammar_mechanics_issue_count": 0,
        "seo_issue_count": 0,
        "publishing_safety_issue_count": 0,
        "body_sentence_count": 0,
        "assertive_sentence_count": 0,
        "claim_covered_sentence_count": 0,
        "uncovered_assertive_sentence_count": 0,
        "uncovered_assertive_sentences": [],
        "body_claim_coverage": 0.0,
        "article_mode": "normal",
        "depth_hard_minimum_words": 0,
        "evidence_word_count": 0,
    }


def build_qa_result(
    *,
    event_id: str | None,
    issues: list[dict[str, str]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    critical = [item for item in issues if item.get("severity") == SEVERITY_CRITICAL]
    warnings = [item for item in issues if item.get("severity") == SEVERITY_WARNING]
    critical_count = len(critical)
    warning_count = len(warnings)
    warning_codes = [
        str(item.get("code") or "")
        for item in warnings
        if isinstance(item, dict) and item.get("code")
    ]
    publishable = critical_count == 0
    return {
        "schema_version": ARTICLE_QA_SCHEMA_VERSION,
        "event_id": event_id,
        "qa_passed": publishable,
        "critical_failures": critical,
        "warnings": warnings,
        "metrics": metrics,
        "publishable": publishable,
        "qa_publishable": publishable,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "warning_codes": warning_codes,
    }
