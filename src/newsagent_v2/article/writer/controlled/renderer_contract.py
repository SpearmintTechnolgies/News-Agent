"""V3.3 renderer contract helpers. Does not change QA or quarantine grounding."""

from __future__ import annotations

import re
from typing import Any

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.controlled.plan import ArticlePlan, ParagraphPlan
from newsagent_v2.article.writer.controlled.proposition import RELATIONSHIP_NONE

FORBIDDEN_IMPLICATION_PHRASES = (
    "which means",
    "therefore",
    "suggesting",
    "signaling",
    "raising concerns",
    "could lead",
    "could push",
    "is likely to",
    "underscores",
    "reflects",
    "highlights",
    "amid broader",
    "as the industry",
    "in a move that",
    "underscored",
    "highlighted the need",
    "might seek",
    "may redirect",
    "may send",
    "could stall",
    "will now",
)

FORBIDDEN_IMPLICATION_RE = re.compile(
    "|".join(re.escape(item) for item in FORBIDDEN_IMPLICATION_PHRASES),
    re.IGNORECASE,
)

INCOMPLETE_ENDINGS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "to",
        "for",
        "toward",
        "towards",
        "such",
        "as",
        "and",
        "or",
        "with",
        "by",
        "in",
        "on",
        "over",
    }
)

UNKNOWN_FACT_ID = "unknown_fact_id"
CROSS_PARAGRAPH_FACT = "cross_paragraph_fact_declaration"
UNAUTHORIZED_RELATIONSHIP = "unauthorized_fact_relationship"
MISSING_FACT_IDS = "missing_fact_ids_used"
MULTI_FACT_ENUMERATION = "MULTI_FACT_ENUMERATION"
AUTHORIZED_RELATIONSHIP = "AUTHORIZED_RELATIONSHIP"
UNAUTHORIZED_RELATIONSHIP_KIND = "UNAUTHORIZED_RELATIONSHIP"

# Paragraph placement is advisory unless locality is semantically required.
_LOCALITY_RELATIONSHIPS = frozenset({"ATTRIBUTION", "CONTRAST"})

# Semantic cues that invent a relationship not independently present in facts.
_CAUSE_RE = re.compile(
    r"\b(?:because|caused|causing|led to|leads to|resulting in|resulted in|due to|"
    r"therefore|consequently|as a result|thereby)\b",
    re.IGNORECASE,
)
_INFERENCE_RE = re.compile(
    r"\b(?:suggesting|showing that|indicating that|meaning that|which implies|"
    r"implies that|which means|implying)\b",
    re.IGNORECASE,
)
_MOTIVATION_RE = re.compile(
    r"\b(?:aimed at|intended to|in order to|seeking to)\b",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"\b(?:outperformed|higher than|lower than|stronger than|weaker than|"
    r"more (?:\w+\s+){0,3}than|less (?:\w+\s+){0,3}than)\b",
    re.IGNORECASE,
)
_CONTRAST_RE = re.compile(
    r"\b(?:despite|although|even though|whereas)\b|"
    r"(?:^|[.;:]\s+)however\b",
    re.IGNORECASE,
)
_DEPENDENT_TEMPORAL_RE = re.compile(
    r"(?:^|[.;:]\s*)(?:after|before|following)\s+(?:this|that|these|those|it|they)\b|"
    r"(?:^|[.;:]\s*)(?:after|before|following)\s+[^,]{3,80},\s+\S",
    re.IGNORECASE,
)
_PREDICTION_RE = re.compile(
    r"\b(?:will cause|could lead(?:\s+to)?|expected to result(?:\s+in)?)\b",
    re.IGNORECASE,
)


def _forbidden_inference_cues(text: str) -> bool:
    """Cause / inference / motive / comparison / prediction — never planner-authorized."""
    return bool(
        _CAUSE_RE.search(text)
        or _INFERENCE_RE.search(text)
        or _MOTIVATION_RE.search(text)
        or _COMPARISON_RE.search(text)
        or _PREDICTION_RE.search(text)
        or implication_language_present(text)
    )


def classify_multi_fact_relationship(
    text: str,
    *,
    fact_ids: list[str],
    paragraph_relationship: str,
) -> str:
    """Classify multi-fact sentences. Multiple authorized IDs alone are not a failure."""
    if len(fact_ids) < 2:
        return AUTHORIZED_RELATIONSHIP
    rel = str(paragraph_relationship or RELATIONSHIP_NONE).upper()
    stripped = str(text or "").strip()
    if _forbidden_inference_cues(stripped):
        return UNAUTHORIZED_RELATIONSHIP_KIND
    if _CONTRAST_RE.search(stripped):
        if rel == "CONTRAST":
            return AUTHORIZED_RELATIONSHIP
        return UNAUTHORIZED_RELATIONSHIP_KIND
    if _DEPENDENT_TEMPORAL_RE.search(stripped):
        if rel == "SEQUENCE":
            return AUTHORIZED_RELATIONSHIP
        return UNAUTHORIZED_RELATIONSHIP_KIND
    if rel in {"SEQUENCE", "ELABORATION", "ATTRIBUTION", "CONTRAST"}:
        return AUTHORIZED_RELATIONSHIP
    # relationship=NONE + multi-fact without inferred-relation cues = enumeration/conjunction.
    return MULTI_FACT_ENUMERATION


def paragraph_fact_budget(plan: ParagraphPlan) -> int:
    if plan.max_factual_assertions is not None:
        return int(plan.max_factual_assertions)
    return len(plan.allowed_claim_ids)


def implication_language_present(text: str) -> bool:
    return bool(FORBIDDEN_IMPLICATION_RE.search(text or ""))


def editorial_truncation_issues(text: str, *, field: str) -> list[dict[str, str]]:
    """Deterministic incomplete-field checks. Not QA. Not a grammar model."""
    stripped = str(text or "").strip().rstrip(".,;:!?")
    if not stripped:
        return [{"field": field, "code": "empty_editorial_field", "message": f"{field} is empty"}]
    last = stripped.split()[-1].lower().strip("\"'")
    issues: list[dict[str, str]] = []
    if last in INCOMPLETE_ENDINGS:
        issues.append(
            {
                "field": field,
                "code": "truncated_terminal_phrase",
                "message": f"{field} ends on an incomplete function word {last!r}",
            }
        )
    parts = stripped.split()
    if len(parts) >= 2 and parts[-2].lower() == "the" and parts[-1][:1].isupper():
        issues.append(
            {
                "field": field,
                "code": "truncated_terminal_phrase",
                "message": f"{field} ends on an unfinished entity phrase {parts[-2]} {parts[-1]!r}",
            }
        )
    if re.search(r"\bsuch as the [A-Z][a-z]+$", stripped):
        if not any(item["code"] == "truncated_terminal_phrase" for item in issues):
            issues.append(
                {
                    "field": field,
                    "code": "truncated_terminal_phrase",
                    "message": f"{field} ends on an unfinished 'such as the …' phrase",
                }
            )
    if field == "headline":
        stacked = re.search(
            r"\bSaid\s+(?:[A-Z][A-Za-z0-9.&-]{1,}\s+){1,}[A-Z][A-Za-z0-9.&-]{1,}\s+Over\b",
            stripped,
        )
        if stacked or (
            " over " in f" {stripped.lower()} "
            and word_count(stripped) >= 8
            and not re.search(r"\b(fails?|failed|wins?|won|beats?|beats)\b", stripped, re.I)
            and stacked is None
            and re.search(r"\bOver\b", stripped)
            and re.search(r"(?:[A-Z][A-Za-z0-9.&-]*\s+){5,}[A-Z]", stripped)
        ):
            issues.append(
                {
                    "field": field,
                    "code": "malformed_headline_relation",
                    "message": "headline stacks nouns around 'over' without a clear grammatical relation",
                }
            )
    return issues


def _declared_sentences(native_paragraph: dict[str, Any]) -> list[dict[str, Any]]:
    rows = native_paragraph.get("sentences")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, dict)]
    return []


def validate_sentence_declarations(
    native: dict[str, Any],
    plan: ArticlePlan,
) -> list[dict[str, str]]:
    """Closed-world accounting. Article-authorized facts may appear in any paragraph."""
    issues: list[dict[str, str]] = []
    by_id = {row.paragraph_id: row for row in plan.paragraph_plans}
    article_claims = set(plan.selected_claim_ids)
    paragraphs = native.get("paragraphs") if isinstance(native.get("paragraphs"), list) else []
    for para in paragraphs:
        if not isinstance(para, dict):
            continue
        pid = str(para.get("paragraph_id") or "")
        para_plan = by_id.get(pid)
        if para_plan is None:
            continue
        allowed = set(para_plan.allowed_claim_ids)
        allowed_quotes = set(para_plan.allowed_quote_ids)
        locality_required = str(getattr(para_plan, "relationship", "") or "") in _LOCALITY_RELATIONSHIPS
        sentences = _declared_sentences(para)
        if not sentences:
            continue
        for item in sentences:
            text = str(item.get("text") or "")
            fact_ids = [str(fid) for fid in (item.get("fact_ids_used") or [])]
            quote_ids = [str(qid) for qid in (item.get("quote_ids_used") or [])]
            if text.strip() and not fact_ids and not quote_ids and word_count(text) >= 8:
                issues.append({"code": MISSING_FACT_IDS, "paragraph_id": pid, "sentence": text})
            for fid in fact_ids:
                if fid not in article_claims:
                    issues.append(
                        {
                            "code": UNKNOWN_FACT_ID,
                            "paragraph_id": pid,
                            "fact_id": fid,
                        }
                    )
                elif locality_required and fid not in allowed:
                    issues.append(
                        {
                            "code": CROSS_PARAGRAPH_FACT,
                            "paragraph_id": pid,
                            "fact_id": fid,
                        }
                    )
            for qid in quote_ids:
                if qid not in allowed_quotes:
                    issues.append({"code": "unauthorized_declared_quote", "paragraph_id": pid, "quote_id": qid})
            relationship = str(getattr(para_plan, "relationship", RELATIONSHIP_NONE) or RELATIONSHIP_NONE)
            if len(fact_ids) >= 2:
                kind = classify_multi_fact_relationship(
                    text,
                    fact_ids=fact_ids,
                    paragraph_relationship=relationship,
                )
                if kind == UNAUTHORIZED_RELATIONSHIP_KIND:
                    issues.append(
                        {
                            "code": UNAUTHORIZED_RELATIONSHIP,
                            "paragraph_id": pid,
                            "sentence": text,
                            "classification": kind,
                        }
                    )
    return issues
