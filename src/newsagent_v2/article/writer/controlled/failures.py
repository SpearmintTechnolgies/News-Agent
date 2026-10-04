"""Deterministic candidate failure classes. Provider errors are not QA failures."""

from __future__ import annotations

from typing import Any

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
WRITER_PROVIDER_ERROR = "WRITER_PROVIDER_ERROR"
WRITER_OUTPUT_INVALID = "WRITER_OUTPUT_INVALID"
GROUNDING_FAILED = "GROUNDING_FAILED"
QUOTE_GROUNDING_FAILED = "QUOTE_GROUNDING_FAILED"
COPYRIGHT_SIMILARITY_FAILED = "COPYRIGHT_SIMILARITY_FAILED"
MECHANICS_FAILED = "MECHANICS_FAILED"
SEO_ONLY_FAILED = "SEO_ONLY_FAILED"
IMAGE_FAILED = "IMAGE_FAILED"
WRITER_PROVIDER_BUDGET_EXCEEDED = "WRITER_PROVIDER_BUDGET_EXCEEDED"
EDITORIAL_CLEANLINESS_FAILED = "EDITORIAL_CLEANLINESS_FAILED"
UNKNOWN_CRITICAL_FAILURE = "UNKNOWN_CRITICAL_FAILURE"

FAILURE_CLASSES = (
    INSUFFICIENT_EVIDENCE,
    WRITER_PROVIDER_ERROR,
    WRITER_PROVIDER_BUDGET_EXCEEDED,
    WRITER_OUTPUT_INVALID,
    GROUNDING_FAILED,
    QUOTE_GROUNDING_FAILED,
    COPYRIGHT_SIMILARITY_FAILED,
    MECHANICS_FAILED,
    SEO_ONLY_FAILED,
    IMAGE_FAILED,
    EDITORIAL_CLEANLINESS_FAILED,
    UNKNOWN_CRITICAL_FAILURE,
)

GROUNDING_CODES = frozenset(
    {
        "body_assertion_not_in_claims",
        "ungrounded_contextual_assertion",
        "unsupported_absence_claim",
        "unauthorized_paragraph_claim",
        "unsupported_paragraph_assertion",
        "ambiguous_paragraph_assertion",
    }
)
QUOTE_CODES = frozenset(
    {
        "quote_body_unmapped",
        "invented_or_modified_quote",
        "unauthorized_paragraph_quote",
        "direct_quote_no_attribution",
        "direct_quote_no_source",
        "quote_missing_evidence",
        "quote_claim_no_evidence",
        "quote_unsupported_evidence",
    }
)
SIMILARITY_CODES = frozenset({"exact_phrase_overlap", "high_sentence_similarity"})
SEO_CRITICAL_CODES = frozenset(
    {
        "seo_title_missing",
        "meta_description_missing",
        "invalid_slug",
        "seo_invalid_category",
    }
)
INSUFFICIENT_CODES = frozenset(
    {
        "insufficient_evidence_for_target_depth",
        "insufficient_evidence",
    }
)


def _codes(qa: dict[str, Any] | None) -> list[str]:
    if not isinstance(qa, dict):
        return []
    return [
        str(item.get("code") or "")
        for item in (qa.get("critical_failures") or [])
        if isinstance(item, dict)
    ]


def classify_qa_failure(qa: dict[str, Any] | None) -> str:
    """Map existing QA criticals. Does not change QA policy."""
    codes = [code for code in _codes(qa) if code]
    if not codes:
        return UNKNOWN_CRITICAL_FAILURE
    unique = set(codes)
    if EDITORIAL_CLEANLINESS_FAILED in unique or "EDITORIAL_CLEANLINESS_FAILED" in unique:
        return EDITORIAL_CLEANLINESS_FAILED
    if unique <= SEO_CRITICAL_CODES:
        return SEO_ONLY_FAILED
    if unique & INSUFFICIENT_CODES:
        return INSUFFICIENT_EVIDENCE
    if unique & QUOTE_CODES:
        return QUOTE_GROUNDING_FAILED
    if unique & GROUNDING_CODES:
        return GROUNDING_FAILED
    if unique & SIMILARITY_CODES:
        return COPYRIGHT_SIMILARITY_FAILED
    if unique - SEO_CRITICAL_CODES:
        return MECHANICS_FAILED
    return UNKNOWN_CRITICAL_FAILURE


def recovery_policy(failure_class: str) -> dict[str, Any]:
    """Bounded, deterministic recovery. No infinite repair loops."""
    if failure_class == INSUFFICIENT_EVIDENCE:
        return {
            "retry_same_candidate": False,
            "seo_normalize": False,
            "eligible_for_deeper_evidence": True,
            "max_deeper_evidence_stages": MAX_DEEPER_EVIDENCE_STAGES_SAFE,
            "note": "Future stage may retrieve additional source evidence. It may not invent evidence.",
        }
    if failure_class == SEO_ONLY_FAILED:
        return {
            "retry_same_candidate": False,
            "seo_normalize": True,
            "eligible_for_deeper_evidence": False,
            "note": "Deterministic SEO correction only. No new factual claims.",
        }
    if failure_class == GROUNDING_FAILED:
        return {
            "retry_same_candidate": False,
            "seo_normalize": False,
            "eligible_for_deeper_evidence": False,
            "note": "Do not magically map unsupported assertions. New output requires a configured bounded render attempt.",
        }
    if failure_class == COPYRIGHT_SIMILARITY_FAILED:
        return {
            "retry_same_candidate": False,
            "seo_normalize": False,
            "eligible_for_deeper_evidence": False,
            "note": "Do not suppress QA. Candidate remains failed under the current attempt.",
        }
    if failure_class == EDITORIAL_CLEANLINESS_FAILED:
        return {
            "retry_same_candidate": False,
            "seo_normalize": False,
            "eligible_for_deeper_evidence": False,
            "note": "Editorial contamination. Do not auto-regenerate. Advance to next candidate.",
        }
    if failure_class == WRITER_PROVIDER_ERROR:
        return {
            "retry_same_candidate": False,
            "seo_normalize": False,
            "eligible_for_deeper_evidence": False,
            "note": "Availability failure is not an editorial/QA failure.",
        }
    if failure_class == WRITER_PROVIDER_BUDGET_EXCEEDED:
        return {
            "retry_same_candidate": False,
            "seo_normalize": False,
            "eligible_for_deeper_evidence": False,
            "note": "Candidate request exceeded provider token budget. No HTTP was sent.",
        }
    return {
        "retry_same_candidate": False,
        "seo_normalize": False,
        "eligible_for_deeper_evidence": False,
        "note": "Candidate remains failed under the current attempt.",
    }


MAX_DEEPER_EVIDENCE_STAGES_SAFE = 0
