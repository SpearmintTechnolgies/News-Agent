"""Optional deterministic post-normalization grounding resolver.

Sits after CanonicalArticle and before QA. Does not call a model.
Does not rewrite article_body. Does not invent claims or evidence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from newsagent_v2.article.expand import evidence_id_index, foreign_evidence_ids

RESOLVED_COVERAGE_KEY = "_resolved_coverage_texts"
MIN_QUOTE_CHARS = 8


def _quote_text(row: dict[str, Any]) -> str:
    return str(row.get("text") or "").strip()


def _quote_in_body(text: str, body: str) -> bool:
    if not text or not body:
        return False
    variants = {text, text.rstrip(","), text.strip("\"“”")}
    return any(item and item in body for item in variants)


def _evidence_ids(row: dict[str, Any]) -> list[str]:
    raw = row.get("evidence_ids")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip() if item is not None else ""
        if text:
            out.append(text)
    return out


def safe_quote_coverage_texts(
    article: dict[str, Any] | None,
    article_input: dict[str, Any] | None,
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return existing quote-ledger texts that may cover body sentences.

    Safe only when the quote already has attribution, frozen evidence IDs,
    and the exact quoted string already appears in article_body.
    """
    if not isinstance(article, dict) or not isinstance(article_input, dict):
        return []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    quotes = article.get("quotes") if isinstance(article.get("quotes"), list) else []
    allowed = evidence_id_index(article_input)
    foreign = foreign_evidence_ids(article_input, other_article_inputs)
    texts: list[str] = []
    for row in quotes:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "direct").lower()
        if kind not in {"direct", "quoted"}:
            continue
        text = _quote_text(row)
        if len(text) < MIN_QUOTE_CHARS:
            continue
        attribution = str(row.get("attribution") or "").strip()
        if not attribution:
            continue
        evidence_ids = _evidence_ids(row)
        if not evidence_ids:
            continue
        if any(eid in foreign for eid in evidence_ids):
            continue
        if any(eid not in allowed for eid in evidence_ids):
            continue
        if not _quote_in_body(text, body):
            continue
        texts.append(text.rstrip(","))
    return texts


def resolve_grounding(
    article: dict[str, Any],
    article_input: dict[str, Any],
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
    ledgers: Any = None,
) -> dict[str, Any]:
    """Return a copy. Never mutates article_body. Does not invent claims or evidence.

    When ledgers is provided, model-generated claim/quote bookkeeping is replaced
    by deterministic maps onto the pre-writing frozen ledgers.
    """
    if ledgers is not None:
        from newsagent_v2.article.writer.ledger_resolve import apply_evidence_ledgers

        return apply_evidence_ledgers(
            article,
            ledgers,
            article_input,
            other_article_inputs=other_article_inputs,
        )
    resolved = deepcopy(article)
    resolved[RESOLVED_COVERAGE_KEY] = safe_quote_coverage_texts(
        resolved,
        article_input,
        other_article_inputs=other_article_inputs,
    )
    return resolved
