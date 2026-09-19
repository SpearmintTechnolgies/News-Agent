from __future__ import annotations

from typing import Any

from newsagent_v2.article.contract import FACTUAL_CLAIM_TYPES
from newsagent_v2.article.input import evidence_index
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue


def check_claims(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    allowed = evidence_index(article_input)
    claims = article.get("claims") if isinstance(article.get("claims"), list) else []
    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []

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

    claim_count = sum(1 for claim in claims if isinstance(claim, dict))
    coverage = (with_evidence / claim_count) if claim_count else 1.0
    metrics = {
        "claim_count": claim_count,
        "claims_with_evidence": with_evidence,
        "evidence_coverage": round(coverage, 4),
        "quote_count": sum(1 for quote in quotes if isinstance(quote, dict)),
    }
    return issues, metrics
