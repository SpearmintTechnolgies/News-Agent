"""Deterministic provider-native → CanonicalArticle. No LLM. No factual repair."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.contract import (
    ARTICLE_CATEGORIES,
    CLAIM_TYPES,
    ENTITY_TYPES,
    QUOTE_KINDS,
    stamp_article_schema_version,
)
from newsagent_v2.article.expand import (
    evidence_id_index,
    expand_provider_article,
    foreign_evidence_ids,
)
from newsagent_v2.article.qa.textutil import split_paragraphs
from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.writer.protocol import FrozenStoryPackage

NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
CODE_MALFORMED = "malformed_provider_result"
CODE_UNKNOWN_EVIDENCE = "unknown_evidence_id"
CODE_FOREIGN_EVIDENCE = "foreign_evidence_id"
CODE_MISSING_PARAGRAPH_MAP = "missing_paragraph_mapping"
CODE_UNKNOWN_CLAIM = "unknown_claim_id"
CODE_EVENT_MISMATCH = "event_id_mismatch"


def _fail(event_id: str, code: str, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "article": None,
        "failure": {
            "event_id": event_id,
            "code": NORMALIZATION_FAILED,
            "detail_code": code,
            "reason": reason,
        },
        "normalized_from": "article_first_provider_native",
    }


def _as_ids(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        text = str(item).strip() if item is not None else ""
        if not text:
            return None
        out.append(text)
    return out


def _claim_id(claim: dict[str, Any]) -> str:
    return str(claim.get("claim_id") or claim.get("id") or "").strip()


def _collect_evidence_ids(row: dict[str, Any]) -> list[str] | None:
    return _as_ids(row.get("evidence_ids"))


def normalize_provider_result(
    native: Any,
    story: FrozenStoryPackage | dict[str, Any],
) -> dict[str, Any]:
    package = story if isinstance(story, FrozenStoryPackage) else FrozenStoryPackage.from_story(story)
    event_id = package.event_id
    if not isinstance(native, dict):
        return _fail(event_id, CODE_MALFORMED, "provider JSON was not an object")

    raw_event = str(native.get("event_id") or "").strip()
    if raw_event and event_id and raw_event != event_id:
        return _fail(event_id, CODE_EVENT_MISMATCH, f"event_id {raw_event!r} does not match {event_id!r}")

    body = native.get("article_body")
    if not isinstance(body, str) or not body.strip():
        return _fail(event_id, CODE_MALFORMED, "article_body is missing")
    paragraphs = split_paragraphs(body)
    if not paragraphs:
        return _fail(event_id, CODE_MALFORMED, "article_body has no paragraphs")

    claims_in = native.get("claims")
    if not isinstance(claims_in, list) or not claims_in:
        return _fail(event_id, CODE_MALFORMED, "claims are missing")

    canonical_claims: list[dict[str, Any]] = []
    claim_index: dict[str, dict[str, Any]] = {}
    local_evidence = evidence_id_index(package.article_input)
    foreign = foreign_evidence_ids(package.article_input, package.other_article_inputs)

    for i, claim in enumerate(claims_in):
        if not isinstance(claim, dict):
            return _fail(event_id, CODE_MALFORMED, f"claims[{i}] is not an object")
        claim_id = _claim_id(claim)
        if not claim_id:
            return _fail(event_id, CODE_MALFORMED, f"claims[{i}] has no id/claim_id")
        if claim_id in claim_index:
            return _fail(event_id, CODE_MALFORMED, f"duplicate claim id {claim_id!r}")
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(event_id, CODE_MALFORMED, f"claims[{i}] text is missing")
        claim_type = claim.get("claim_type")
        if claim_type not in CLAIM_TYPES:
            return _fail(event_id, CODE_MALFORMED, f"claims[{i}] claim_type is invalid")
        evidence_ids = _collect_evidence_ids(claim)
        if not evidence_ids:
            return _fail(event_id, CODE_MALFORMED, f"claims[{i}] evidence_ids are missing")
        for evidence_id in evidence_ids:
            if evidence_id in foreign:
                return _fail(event_id, CODE_FOREIGN_EVIDENCE, f"foreign evidence id {evidence_id!r}")
            if evidence_id not in local_evidence:
                return _fail(event_id, CODE_UNKNOWN_EVIDENCE, f"unknown evidence id {evidence_id!r}")
        row = {
            "claim_id": claim_id,
            "text": text.strip(),
            "claim_type": claim_type,
            "evidence_ids": list(evidence_ids),
        }
        canonical_claims.append(row)
        claim_index[claim_id] = row

    maps = native.get("paragraph_maps")
    if not isinstance(maps, list) or not maps:
        return _fail(event_id, CODE_MISSING_PARAGRAPH_MAP, "paragraph_maps are missing")

    by_index: dict[int, list[str]] = {}
    for i, row in enumerate(maps):
        if not isinstance(row, dict):
            return _fail(event_id, CODE_MALFORMED, f"paragraph_maps[{i}] is not an object")
        try:
            index = int(row.get("paragraph_index"))
        except (TypeError, ValueError):
            return _fail(event_id, CODE_MALFORMED, f"paragraph_maps[{i}] paragraph_index is invalid")
        if index in by_index:
            return _fail(event_id, CODE_MALFORMED, f"duplicate paragraph_index {index}")
        claim_ids = _as_ids(row.get("claim_ids"))
        if not claim_ids:
            return _fail(event_id, CODE_MISSING_PARAGRAPH_MAP, f"paragraph_maps[{i}] claim_ids are missing")
        for claim_id in claim_ids:
            if claim_id not in claim_index:
                return _fail(event_id, CODE_UNKNOWN_CLAIM, f"paragraph map cites unknown claim {claim_id!r}")
        by_index[index] = claim_ids

    for index, _text in enumerate(paragraphs):
        if index not in by_index:
            return _fail(
                event_id,
                CODE_MISSING_PARAGRAPH_MAP,
                f"paragraph_index {index} has no mapping",
            )
    extra = [index for index in by_index if index < 0 or index >= len(paragraphs)]
    if extra:
        return _fail(
            event_id,
            CODE_MISSING_PARAGRAPH_MAP,
            f"paragraph_index {extra[0]} does not match article_body paragraphs",
        )

    quotes_in = native.get("quotes") if isinstance(native.get("quotes"), list) else []
    canonical_quotes: list[dict[str, Any]] = []
    for i, quote in enumerate(quotes_in):
        if not isinstance(quote, dict):
            return _fail(event_id, CODE_MALFORMED, f"quotes[{i}] is not an object")
        text = quote.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail(event_id, CODE_MALFORMED, f"quotes[{i}] text is missing")
        kind = quote.get("kind")
        if kind not in QUOTE_KINDS:
            return _fail(event_id, CODE_MALFORMED, f"quotes[{i}] kind is invalid")
        attribution = quote.get("attribution")
        if not isinstance(attribution, str):
            return _fail(event_id, CODE_MALFORMED, f"quotes[{i}] attribution is missing")
        evidence_ids = _collect_evidence_ids(quote)
        if not evidence_ids:
            return _fail(event_id, CODE_MALFORMED, f"quotes[{i}] evidence_ids are missing")
        for evidence_id in evidence_ids:
            if evidence_id in foreign:
                return _fail(event_id, CODE_FOREIGN_EVIDENCE, f"foreign evidence id {evidence_id!r}")
            if evidence_id not in local_evidence:
                return _fail(event_id, CODE_UNKNOWN_EVIDENCE, f"unknown evidence id {evidence_id!r}")
        canonical_quotes.append(
            {
                "text": text,
                "kind": kind,
                "attribution": attribution,
                "evidence_ids": list(evidence_ids),
            }
        )

    for name in ("headline", "dek", "seo_title", "meta_description", "slug"):
        if not isinstance(native.get(name), str) or not str(native.get(name)).strip():
            return _fail(event_id, CODE_MALFORMED, f"{name} is missing")
    category = native.get("category")
    if category not in ARTICLE_CATEGORIES:
        return _fail(event_id, CODE_MALFORMED, "category is invalid")
    entities = native.get("entities")
    if not isinstance(entities, list):
        return _fail(event_id, CODE_MALFORMED, "entities are missing")
    canonical_entities: list[dict[str, Any]] = []
    for i, entity in enumerate(entities):
        if not isinstance(entity, dict):
            return _fail(event_id, CODE_MALFORMED, f"entities[{i}] is not an object")
        name = entity.get("name")
        ent_type = entity.get("type")
        if not isinstance(name, str) or not name.strip() or ent_type not in ENTITY_TYPES:
            return _fail(event_id, CODE_MALFORMED, f"entities[{i}] is invalid")
        canonical_entities.append({"name": name.strip(), "type": ent_type})
    keywords = native.get("keywords")
    if not isinstance(keywords, list) or any(not isinstance(item, str) or not item.strip() for item in keywords):
        return _fail(event_id, CODE_MALFORMED, "keywords are invalid")

    sections = []
    for index, text in enumerate(paragraphs):
        sections.append(
            {
                "id": f"s{index + 1}",
                "paragraphs": [{"text": text, "claim_ids": list(by_index[index])}],
            }
        )

    article: dict[str, Any] = {
        "event_id": event_id or raw_event,
        "headline": str(native["headline"]).strip(),
        "dek": str(native["dek"]).strip(),
        "article_body": body,
        "category": category,
        "seo_title": str(native["seo_title"]).strip(),
        "meta_description": str(native["meta_description"]).strip(),
        "slug": str(native["slug"]).strip(),
        "entities": canonical_entities,
        "keywords": [str(item).strip() for item in keywords],
        "claims": canonical_claims,
        "quotes": canonical_quotes,
        "article_sections": sections,
        "paragraph_maps": [
            {"paragraph_index": index, "claim_ids": list(by_index[index])}
            for index in range(len(paragraphs))
        ],
    }
    stamp_article_schema_version(article)
    expand_provider_article(article, package.article_input)
    materialize_article(article)
    return {
        "ok": True,
        "article": article,
        "failure": None,
        "normalized_from": "article_first_provider_native",
        "provider_claim_ids_renamed": True,
    }


def normalize_article_first(
    parsed: Any,
    *,
    event_id: str,
    story: dict[str, Any],
) -> dict[str, Any]:
    """Bake-off compatible wrapper."""
    pack = dict(story)
    pack["event_id"] = event_id or pack.get("event_id")
    result = normalize_provider_result(parsed, pack)
    return {
        "article": result.get("article"),
        "failure": result.get("failure"),
        "normalized_from": result.get("normalized_from"),
        "ok": result.get("ok"),
    }
