"""
Deterministic generation-contract checks for structured articles.

Runs before ordinary QA. Does not invent missing mappings.
Legacy articles without article_sections are skipped (old free-form bodies).
"""

from __future__ import annotations

import re
from typing import Any

from newsagent_v2.article.expand import evidence_id_index, foreign_evidence_ids
from newsagent_v2.article.input import evidence_index
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue
from newsagent_v2.article.render import iter_section_paragraphs

QUOTE_MARKS = frozenset({"\"", "“", "”"})
_QUOTE_SPAN_RE = re.compile(r"[\"“]([^\"”]+)[\"”]")


def extract_quoted_spans(text: str) -> list[str]:
    return [match.group(1).strip() for match in _QUOTE_SPAN_RE.finditer(text or "") if match.group(1).strip()]


def _normalize_quote_text(text: str) -> str:
    return str(text or "").strip().strip("\"“”").strip()


def _claim_ids(article: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for claim in article.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id") or claim.get("id") or "").strip()
        if claim_id:
            found[claim_id] = claim
    return found


def _paragraph_has_quote_marks(text: str) -> bool:
    return any(mark in text for mark in QUOTE_MARKS)


def _compact_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _quote_span_mapped(span: str, quote_texts: list[str]) -> bool:
    wanted = _normalize_quote_text(span)
    if not wanted:
        return True
    return any(_normalize_quote_text(quote) == wanted for quote in quote_texts)


def check_structure(
    article: dict[str, Any],
    article_input: dict[str, Any],
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    sections = article.get("article_sections")
    if not isinstance(sections, list) or not sections:
        return []

    issues: list[dict[str, Any]] = []
    allowed = evidence_index(article_input)
    this_ids = evidence_id_index(article_input)
    foreign_ids = foreign_evidence_ids(article_input, other_article_inputs)
    foreign_urls: set[str] = set()
    for other in other_article_inputs or []:
        if not isinstance(other, dict):
            continue
        if other.get("event_id") == article_input.get("event_id"):
            continue
        foreign_urls.update(evidence_index(other).keys())

    claims_by_id = _claim_ids(article)
    referenced: set[str] = set()
    paragraphs = iter_section_paragraphs(article)
    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []
    quote_texts = [
        str(row.get("text") or "").strip()
        for row in quotes
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]

    for index, paragraph in enumerate(paragraphs):
        text = str(paragraph.get("text") or "").strip()
        claim_ids = paragraph.get("claim_ids")
        if not isinstance(claim_ids, list) or not [str(item).strip() for item in claim_ids if str(item).strip()]:
            issues.append(
                issue(
                    code="paragraph_missing_claim_ids",
                    message=f"article paragraph[{index}] has no claim_ids",
                    severity=SEVERITY_CRITICAL,
                    module="structure",
                )
            )
            continue
        for raw in claim_ids:
            claim_id = str(raw).strip()
            if not claim_id:
                continue
            referenced.add(claim_id)
            if claim_id not in claims_by_id:
                issues.append(
                    issue(
                        code="unknown_claim_id",
                        message=f"paragraph[{index}] references unknown claim_id {claim_id}",
                        severity=SEVERITY_CRITICAL,
                        module="structure",
                    )
                )
        if _paragraph_has_quote_marks(text):
            spans = extract_quoted_spans(text)
            mapped = bool(spans) and all(_quote_span_mapped(span, quote_texts) for span in spans)
            if not mapped:
                issues.append(
                    issue(
                        code="quote_body_unmapped",
                        message=(
                            f"paragraph[{index}] contains quote marks but no matching quotes[] entry"
                        ),
                        severity=SEVERITY_CRITICAL,
                        module="structure",
                    )
                )

    for claim_id, claim in claims_by_id.items():
        compact_ids = _compact_ids(claim.get("evidence_ids"))
        refs = claim.get("evidence_refs")
        if compact_ids:
            for evidence_id in compact_ids:
                if evidence_id in this_ids:
                    continue
                code = "foreign_evidence_ref" if evidence_id in foreign_ids else "unknown_evidence_ref"
                issues.append(
                    issue(
                        code=code,
                        message=f"claim {claim_id} evidence_id is not in this story's evidence",
                        severity=SEVERITY_CRITICAL,
                        module="structure",
                    )
                )
        elif not isinstance(refs, list) or not refs:
            issues.append(
                issue(
                    code="claim_missing_evidence",
                    message=f"claim {claim_id} has no evidence_ids",
                    severity=SEVERITY_CRITICAL,
                    module="structure",
                )
            )
        else:
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                url = ref.get("url")
                if not isinstance(url, str) or not url:
                    issues.append(
                        issue(
                            code="unknown_evidence_ref",
                            message=f"claim {claim_id} has an empty evidence url",
                            severity=SEVERITY_CRITICAL,
                            module="structure",
                        )
                    )
                    continue
                if url not in allowed:
                    code = "foreign_evidence_ref" if url in foreign_urls else "unknown_evidence_ref"
                    issues.append(
                        issue(
                            code=code,
                            message=f"claim {claim_id} evidence_ref is not in this story's evidence",
                            severity=SEVERITY_CRITICAL,
                            module="structure",
                        )
                    )
        if claim_id not in referenced:
            issues.append(
                issue(
                    code="orphan_claim",
                    message=f"claim {claim_id} is not referenced by any body paragraph",
                    severity=SEVERITY_CRITICAL,
                    module="structure",
                )
            )

    return issues
