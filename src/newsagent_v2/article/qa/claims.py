from __future__ import annotations

from typing import Any

from newsagent_v2.article.contract import FACTUAL_CLAIM_TYPES
from newsagent_v2.article.input import evidence_index
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import words


def _normalized_tokens(text: str) -> set[str]:
    return set(words(text))


def _quote_supported_by_evidence(
    quote_text: str,
    refs: list[Any],
    allowed: dict[str, dict[str, Any]],
) -> bool:
    quote_tokens = _normalized_tokens(quote_text)
    if not quote_tokens:
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        row = allowed.get(ref.get("url")) if isinstance(ref.get("url"), str) else None
        if not isinstance(row, dict):
            continue
        evidence = " ".join(
            str(row.get(key) or "")
            for key in ("title", "summary", "extracted_text")
        )
        evidence_tokens = _normalized_tokens(evidence)
        if quote_tokens <= evidence_tokens:
            return True
        if len(quote_tokens & evidence_tokens) / len(quote_tokens) >= 0.6:
            return True
    return False


def check_claims(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    allowed = evidence_index(article_input)
    claims = article.get("claims") if isinstance(article.get("claims"), list) else []
    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []

    quote_rows_by_text = {
        str(row.get("text") or "").strip(): row
        for row in quotes
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    }
    body = str(article.get("article_body") or "")
    # Match editorial cleanliness: ignore Conclusion/FAQ restatements for
    # unmapped nickname/token quotes (e.g. Tieshun "Pacman" Roquerre).
    from newsagent_v2.article.qa.editorial_cleanliness import _body_without_closing_sections

    body_for_quote_map = _body_without_closing_sections(body)
    _unmapped_spans: list[str] = []
    for span in extract_quoted_spans(body_for_quote_map):
        row = quote_rows_by_text.get(span)
        if not isinstance(row, dict):
            _unmapped_spans.append(span)
            issues.append(
                issue(
                    code="quote_body_unmapped",
                    message="article_body contains a quoted span without a matching quotes[] entry",
                    severity=SEVERITY_CRITICAL,
                    module="claims",
                )
            )
    # #region agent log
    if _unmapped_spans:
        try:
            import json as _json, time as _time
            from pathlib import Path as _Path
            _payload = {
                "sessionId": "7f9dc8",
                "hypothesisId": "D",
                "location": "claims.py:check_claims",
                "message": "unmapped_quote_spans",
                "data": {
                    "unmapped": _unmapped_spans[:20],
                    "mapped_quote_count": len(quote_rows_by_text),
                    "span_count": len(extract_quoted_spans(body)),
                },
                "timestamp": int(_time.time() * 1000),
            }
            with (_Path("debug-7f9dc8.log")).open("a", encoding="utf-8") as _f:
                _f.write(_json.dumps(_payload) + "\n")
        except Exception:
            pass
    # #endregion

    with_evidence = 0
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        refs = claim.get("evidence_refs")
        has_ref = isinstance(refs, list) and bool(refs)
        if has_ref:
            with_evidence += 1
        claim_type = claim.get("claim_type")
        if claim_type in FACTUAL_CLAIM_TYPES and not has_ref:
            issues.append(
                issue(
                    code="claim_missing_evidence",
                    message=f"claims[{i}] factual claim has no evidence_refs",
                    severity=SEVERITY_CRITICAL,
                    module="claims",
                )
            )
        if claim_type == "number" and not has_ref:
            issues.append(
                issue(
                    code="numeric_claim_no_evidence",
                    message=f"claims[{i}] numeric claim has zero evidence refs",
                    severity=SEVERITY_CRITICAL,
                    module="claims",
                )
            )
        if claim_type == "quote" and not has_ref:
            issues.append(
                issue(
                    code="quote_claim_no_evidence",
                    message=f"claims[{i}] quote claim requires evidence",
                    severity=SEVERITY_CRITICAL,
                    module="claims",
                )
            )
        if isinstance(refs, list):
            for j, ref in enumerate(refs):
                if not isinstance(ref, dict):
                    continue
                url = ref.get("url")
                if isinstance(url, str) and url not in allowed:
                    issues.append(
                        issue(
                            code="claim_unknown_evidence",
                            message=f"claims[{i}] evidence_refs[{j}] is not in the pack",
                            severity=SEVERITY_CRITICAL,
                            module="claims",
                        )
                    )

    for i, quote in enumerate(quotes):
        if not isinstance(quote, dict):
            continue
        refs = quote.get("evidence_refs")
        has_ref = isinstance(refs, list) and bool(refs)
        if quote.get("kind") in {"direct", "paraphrase"} and not has_ref:
            issues.append(
                issue(
                    code="quote_missing_evidence",
                    message=f"quotes[{i}] requires evidence_refs",
                    severity=SEVERITY_CRITICAL,
                    module="claims",
                )
            )
        if quote.get("kind") == "direct":
            attribution = quote.get("attribution")
            if not isinstance(attribution, str) or not attribution.strip():
                issues.append(
                    issue(
                        code="direct_quote_no_attribution",
                        message=f"quotes[{i}] direct quote requires attribution",
                        severity=SEVERITY_CRITICAL,
                        module="claims",
                    )
                )
            if not has_ref:
                issues.append(
                    issue(
                        code="direct_quote_no_source",
                        message=f"quotes[{i}] direct quote requires a source evidence ref",
                        severity=SEVERITY_CRITICAL,
                        module="claims",
                    )
                )
        if has_ref and isinstance(refs, list):
            quote_text = str(quote.get("text") or "").strip()
            if not _quote_supported_by_evidence(quote_text, refs, allowed):
                issues.append(
                    issue(
                        code="quote_unsupported_evidence",
                        message=f"quotes[{i}] text is not supported by referenced evidence",
                        severity=SEVERITY_CRITICAL,
                        module="claims",
                    )
                )

    claim_count = sum(1 for claim in claims if isinstance(claim, dict))
    coverage = (with_evidence / claim_count) if claim_count else 1.0
    metrics = {
        "claim_count": claim_count,
        "claims_with_evidence": with_evidence,
        "evidence_coverage": round(coverage, 4),
        "quote_count": sum(1 for quote in quotes if isinstance(quote, dict)),
    }
    return issues, metrics
