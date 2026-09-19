"""Map body assertions to a pre-writing evidence ledger. May MAP. Must not invent support."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from newsagent_v2.article.expand import expand_provider_article
from newsagent_v2.article.qa.grounding import (
    _overlapping_claims,
    is_connective_sentence,
    sentence_covered_by_claims,
    sentence_matches_claim,
)
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import split_paragraphs, split_sentences
from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote
from newsagent_v2.article.writer.grounding_resolve import (
    RESOLVED_COVERAGE_KEY,
    safe_quote_coverage_texts,
)

_QUOTE_STRIP = "\"“”'"


def _norm_quote(text: str) -> str:
    return str(text or "").strip().strip(_QUOTE_STRIP).strip()


def matching_ledger_claims(sentence: str, ledgers: EvidenceLedgers) -> list[LedgerClaim]:
    """High-confidence matches only. Does not create claims."""
    exact = [row for row in ledgers.claims if sentence_matches_claim(sentence, row.text)]
    if exact:
        return exact
    texts = ledgers.claim_texts()
    if not sentence_covered_by_claims(sentence, texts):
        return []
    overlapping = set(_overlapping_claims(sentence, texts))
    return [row for row in ledgers.claims if row.text in overlapping]


def matching_ledger_quotes(span: str, ledgers: EvidenceLedgers) -> list[LedgerQuote]:
    wanted = _norm_quote(span)
    if not wanted:
        return []
    return [row for row in ledgers.quotes if _norm_quote(row.text) == wanted]


def apply_evidence_ledgers(
    article: dict[str, Any],
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replace model bookkeeping with ledger mappings. Never rewrites article_body."""
    resolved = deepcopy(article)
    body = resolved.get("article_body") if isinstance(resolved.get("article_body"), str) else ""
    original_body = body
    paragraphs = split_paragraphs(body)
    used: dict[str, LedgerClaim] = {}
    used_quotes: dict[str, LedgerQuote] = {}
    maps: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []

    for index, paragraph in enumerate(paragraphs):
        claim_ids: list[str] = []
        for sentence in split_sentences(paragraph):
            if is_connective_sentence(sentence):
                continue
            for claim in matching_ledger_claims(sentence, ledgers):
                used[claim.claim_id] = claim
                if claim.claim_id not in claim_ids:
                    claim_ids.append(claim.claim_id)
        for span in extract_quoted_spans(paragraph):
            for quote in matching_ledger_quotes(span, ledgers):
                used_quotes[quote.quote_id] = quote
        maps.append({"paragraph_index": index, "claim_ids": list(claim_ids)})
        sections.append(
            {
                "id": f"s{index + 1}",
                "section_id": f"s{index + 1}",
                "purpose": "",
                "paragraphs": [{"text": paragraph, "claim_ids": list(claim_ids)}],
            }
        )

    resolved["claims"] = [
        {
            "claim_id": row.claim_id,
            "id": row.claim_id,
            "text": row.text,
            "claim_type": row.claim_type,
            "evidence_ids": list(row.evidence_ids),
        }
        for row in ledgers.claims
        if row.claim_id in used
    ]
    resolved["quotes"] = [
        {
            "quote_id": row.quote_id,
            "text": row.text,
            "kind": "direct",
            "attribution": row.speaker,
            "evidence_ids": [row.evidence_id],
        }
        for row in ledgers.quotes
        if row.quote_id in used_quotes
    ]
    resolved["paragraph_maps"] = maps
    resolved["article_sections"] = sections
    expand_provider_article(resolved, article_input, other_article_inputs=other_article_inputs)
    materialize_article(resolved)
    resolved["article_body"] = original_body
    resolved[RESOLVED_COVERAGE_KEY] = safe_quote_coverage_texts(
        resolved,
        article_input,
        other_article_inputs=other_article_inputs,
    )
    resolved["_ledger_mapped_claim_ids"] = sorted(used)
    resolved["_ledger_mapped_quote_ids"] = sorted(used_quotes)
    return resolved


def canonical_from_ledger_first_native(
    native: dict[str, Any],
    *,
    article_input: dict[str, Any],
    ledgers: EvidenceLedgers,
) -> dict[str, Any]:
    """Build CanonicalArticle from ledger-first prose. Does not invent claims."""
    from newsagent_v2.article.contract import (
        ARTICLE_CATEGORIES,
        ENTITY_TYPES,
        stamp_article_schema_version,
    )

    required = (
        "event_id",
        "headline",
        "dek",
        "article_body",
        "category",
        "seo_title",
        "meta_description",
        "slug",
    )
    for name in required:
        if not isinstance(native.get(name), str) or not str(native.get(name)).strip():
            raise ValueError(f"{name} is missing")
    category = native.get("category")
    if category not in ARTICLE_CATEGORIES:
        raise ValueError("category is invalid")
    entities_in = native.get("entities")
    if not isinstance(entities_in, list):
        raise ValueError("entities are missing")
    entities: list[dict[str, Any]] = []
    for item in entities_in:
        if not isinstance(item, dict):
            raise ValueError("entity is invalid")
        name = item.get("name")
        ent_type = item.get("type")
        if not isinstance(name, str) or not name.strip() or ent_type not in ENTITY_TYPES:
            raise ValueError("entity is invalid")
        entities.append({"name": name.strip(), "type": ent_type})
    keywords = native.get("keywords")
    if not isinstance(keywords, list) or any(not isinstance(item, str) or not item.strip() for item in keywords):
        raise ValueError("keywords are invalid")
    article = {
        "event_id": str(native["event_id"]).strip(),
        "headline": str(native["headline"]).strip(),
        "dek": str(native["dek"]).strip(),
        "article_body": str(native["article_body"]).strip(),
        "category": category,
        "seo_title": str(native["seo_title"]).strip(),
        "meta_description": str(native["meta_description"]).strip(),
        "slug": str(native["slug"]).strip(),
        "entities": entities,
        "keywords": [str(item).strip() for item in keywords],
        "claims": [],
        "quotes": [],
        "article_sections": [],
        "paragraph_maps": [],
    }
    stamp_article_schema_version(article)
    return apply_evidence_ledgers(article, ledgers, article_input)
