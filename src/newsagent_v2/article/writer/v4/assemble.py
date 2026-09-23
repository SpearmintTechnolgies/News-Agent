"""V4 assemble CanonicalArticle from natural prose + ledgers. No ParagraphPlan."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.writer.canonical import CANONICAL_ARTICLE_FIELDS
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.v4.atomic_grounding import build_authorized_proposition_set
from newsagent_v2.article.writer.v4.verify import VerificationReport
from newsagent_v2.article.writer.v4.closing_sections import append_grounded_closing_sections
from newsagent_v2.article.writer.v4.writer import V4NativeArticle


def assemble_v4_article(
    *,
    event_id: str,
    native: V4NativeArticle,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    report: VerificationReport,
) -> dict[str, Any]:
    """Assemble article with the COMPLETE authorized claim/proposition set.

    Authorized evidence is independent of whether an earlier matcher recognized
    a particular article realization — never build claims only from matched rows.
    """
    del report  # verification diagnostics live separately; claims are not match-gated
    claims = ledgers.as_claim_dicts()
    authorized = build_authorized_proposition_set(ledgers=ledgers)
    quotes = ledgers.as_quote_dicts()
    category = str(article_input.get("category") or "other")
    body, closing_meta = append_grounded_closing_sections(
        native.article_body,
        authorized_propositions=authorized.propositions,
        article_type=str(article_input.get("article_type") or article_input.get("depth_article_type") or ""),
    )
    article: dict[str, Any] = {
        "schema_version": "article-output-v1",
        "event_id": event_id,
        "headline": native.headline,
        "dek": native.dek,
        "article_body": body,
        "category": category,
        "seo_title": native.seo_title or native.headline,
        "meta_description": native.meta_description or native.dek,
        "slug": native.slug,
        "entities": [],
        "keywords": [],
        "claims": claims,
        "authorized_propositions": authorized.as_dict(),
        "quotes": quotes,
        "article_sections": [],
        "paragraph_maps": [],
        "evidence_used": [],
        "generation_notes": "v4_natural_prose",
        "closing_sections": closing_meta,
        "architecture": "v4",
    }
    for field in CANONICAL_ARTICLE_FIELDS:
        article.setdefault(field, article.get(field))
    materialize_article(article)
    return article
