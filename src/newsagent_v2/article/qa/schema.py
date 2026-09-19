from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from newsagent_v2.article.contract import (
    ARTICLE_CATEGORIES,
    ARTICLE_OUTPUT_SCHEMA_VERSION,
    CLAIM_TYPES,
    ENTITY_TYPES,
    QUOTE_KINDS,
    REQUIRED_ARTICLE_FIELDS,
    REQUIRED_CLAIM_FIELDS,
    REQUIRED_ENTITY_FIELDS,
    REQUIRED_EVIDENCE_REF_FIELDS,
    REQUIRED_QUOTE_FIELDS,
)
from newsagent_v2.article.input import evidence_index
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def check_schema(article: Any, article_input: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(article, dict):
        return [
            issue(
                code="schema_not_object",
                message="article output must be a JSON object",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        ]

    for field in REQUIRED_ARTICLE_FIELDS:
        if field not in article:
            issues.append(
                issue(
                    code="missing_field",
                    message=f"missing required field: {field}",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
    if issues:
        return issues

    if article.get("schema_version") != ARTICLE_OUTPUT_SCHEMA_VERSION:
        issues.append(
            issue(
                code="schema_version",
                message="schema_version must be article-output-v1",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )

    expected_event = article_input.get("event_id")
    event_id = article.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        issues.append(
            issue(
                code="invalid_event_id",
                message="event_id must be a non-empty string",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )
    elif expected_event and event_id != expected_event:
        issues.append(
            issue(
                code="event_id_mismatch",
                message=f"event_id {event_id!r} does not match input {expected_event!r}",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )

    category = article.get("category")
    if category not in ARTICLE_CATEGORIES:
        issues.append(
            issue(
                code="invalid_category",
                message="category is not in the closed article category set",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )

    for name in ("headline", "dek", "article_body", "seo_title", "meta_description", "slug"):
        if not isinstance(article.get(name), str):
            issues.append(
                issue(
                    code="invalid_type",
                    message=f"{name} must be a string",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )

    for name in ("entities", "keywords", "evidence_used", "claims", "quotes"):
        if not isinstance(article.get(name), list):
            issues.append(
                issue(
                    code="invalid_type",
                    message=f"{name} must be a list",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )

    allowed = evidence_index(article_input)

    for i, ref in enumerate(article.get("evidence_used") or []):
        issues.extend(_check_evidence_ref(ref, i, "evidence_used", allowed))

    for i, entity in enumerate(article.get("entities") or []):
        if not isinstance(entity, dict):
            issues.append(
                issue(
                    code="invalid_entity",
                    message=f"entities[{i}] must be an object",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
            continue
        missing = [field for field in REQUIRED_ENTITY_FIELDS if field not in entity]
        if missing:
            issues.append(
                issue(
                    code="invalid_entity",
                    message=f"entities[{i}] missing {', '.join(missing)}",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
            continue
        if not isinstance(entity.get("name"), str) or not entity["name"].strip():
            issues.append(
                issue(
                    code="invalid_entity",
                    message=f"entities[{i}] name must be a non-empty string",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
        if entity.get("type") not in ENTITY_TYPES:
            issues.append(
                issue(
                    code="invalid_entity_type",
                    message=f"entities[{i}] type is not in the closed set",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )

    for i, keyword in enumerate(article.get("keywords") or []):
        if not isinstance(keyword, str) or not keyword.strip():
            issues.append(
                issue(
                    code="invalid_keyword",
                    message=f"keywords[{i}] must be a non-empty string",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )

    for i, claim in enumerate(article.get("claims") or []):
        if not isinstance(claim, dict):
            issues.append(
                issue(
                    code="invalid_claim",
                    message=f"claims[{i}] must be an object",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
            continue
        missing = [field for field in REQUIRED_CLAIM_FIELDS if field not in claim]
        if missing:
            issues.append(
                issue(
                    code="invalid_claim",
                    message=f"claims[{i}] missing {', '.join(missing)}",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
            continue
        if claim.get("claim_type") not in CLAIM_TYPES:
            issues.append(
                issue(
                    code="invalid_claim_type",
                    message=f"claims[{i}] claim_type is not in the closed set",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
        if not isinstance(claim.get("evidence_refs"), list):
            issues.append(
                issue(
                    code="invalid_claim_refs",
                    message=f"claims[{i}] evidence_refs must be a list",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
        else:
            for j, ref in enumerate(claim["evidence_refs"]):
                issues.extend(
                    _check_evidence_ref(ref, f"{i}.{j}", "claims", allowed)
                )

    for i, quote in enumerate(article.get("quotes") or []):
        if not isinstance(quote, dict):
            issues.append(
                issue(
                    code="invalid_quote",
                    message=f"quotes[{i}] must be an object",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
            continue
        missing = [field for field in REQUIRED_QUOTE_FIELDS if field not in quote]
        if missing:
            issues.append(
                issue(
                    code="invalid_quote",
                    message=f"quotes[{i}] missing {', '.join(missing)}",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
            continue
        if quote.get("kind") not in QUOTE_KINDS:
            issues.append(
                issue(
                    code="invalid_quote_kind",
                    message=f"quotes[{i}] kind must be direct or paraphrase",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
        if not isinstance(quote.get("evidence_refs"), list):
            issues.append(
                issue(
                    code="invalid_quote_refs",
                    message=f"quotes[{i}] evidence_refs must be a list",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
        else:
            for j, ref in enumerate(quote["evidence_refs"]):
                issues.extend(
                    _check_evidence_ref(ref, f"{i}.{j}", "quotes", allowed)
                )

    return issues


def _check_evidence_ref(
    ref: Any,
    index: Any,
    group: str,
    allowed: dict[str, dict],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(ref, dict):
        return [
            issue(
                code="invalid_evidence_ref",
                message=f"{group}[{index}] evidence ref must be an object",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        ]
    missing = [field for field in REQUIRED_EVIDENCE_REF_FIELDS if field not in ref]
    if missing:
        issues.append(
            issue(
                code="invalid_evidence_ref",
                message=f"{group}[{index}] missing {', '.join(missing)}",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )
        return issues
    url = ref.get("url")
    if not isinstance(url, str) or not _valid_http_url(url):
        issues.append(
            issue(
                code="invalid_evidence_url",
                message=f"{group}[{index}] url is not a valid http(s) URL",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )
        return issues
    if url not in allowed:
        issues.append(
            issue(
                code="invented_evidence_ref",
                message=f"{group}[{index}] url is not in the evidence pack",
                severity=SEVERITY_CRITICAL,
                module="schema",
            )
        )
    source = ref.get("source")
    if "source" in ref:
        if not isinstance(source, str) or not source.strip():
            issues.append(
                issue(
                    code="invalid_evidence_source",
                    message=f"{group}[{index}] source must be a non-empty string if present",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
        elif url in allowed and allowed[url].get("source") != source:
            issues.append(
                issue(
                    code="evidence_source_mismatch",
                    message=f"{group}[{index}] source does not match the evidence pack",
                    severity=SEVERITY_CRITICAL,
                    module="schema",
                )
            )
    return issues
