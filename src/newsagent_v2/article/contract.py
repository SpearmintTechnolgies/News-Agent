"""
article-output-v1: provider-independent CoinNetwork article contract.

Designed for one-pass generation plus deterministic QA. Word counts and
similarity are computed in Python, not by a model.
"""

from __future__ import annotations

from newsagent_v2.benchmark.contract import SEMANTIC_CATEGORIES

ARTICLE_INPUT_SCHEMA_VERSION = "article-input-v1"
ARTICLE_OUTPUT_SCHEMA_VERSION = "article-output-v1"
ARTICLE_QA_SCHEMA_VERSION = "article-qa-v1"

ARTICLE_CATEGORIES = SEMANTIC_CATEGORIES

CLAIM_TYPES = frozenset(
    {
        "fact",
        "number",
        "date",
        "attribution",
        "quote",
        "contextual",
        "analysis",
    }
)

QUOTE_KINDS = frozenset({"direct", "paraphrase"})

ENTITY_TYPES = frozenset(
    {
        "person",
        "org",
        "product",
        "place",
        "other",
    }
)

FACTUAL_CLAIM_TYPES = frozenset(
    {
        "fact",
        "number",
        "date",
        "attribution",
        "quote",
    }
)

REQUIRED_ARTICLE_FIELDS = (
    "schema_version",
    "event_id",
    "headline",
    "dek",
    "article_body",
    "category",
    "seo_title",
    "meta_description",
    "slug",
    "entities",
    "keywords",
    "evidence_used",
    "claims",
    "quotes",
)

REQUIRED_CLAIM_FIELDS = (
    "claim_id",
    "text",
    "claim_type",
    "evidence_refs",
)

REQUIRED_QUOTE_FIELDS = (
    "text",
    "kind",
    "attribution",
    "evidence_refs",
)


def stamp_article_schema_version(article: dict) -> dict:
    """Assign article-output-v1 locally. Groq must not be asked to emit this constant."""
    if article.get("schema_version") in {None, ""}:
        article["schema_version"] = ARTICLE_OUTPUT_SCHEMA_VERSION
    return article

REQUIRED_EVIDENCE_REF_FIELDS = ("url",)

REQUIRED_ENTITY_FIELDS = ("name", "type")

FUTURE_QA_HOOKS = (
    "semantic_claim_entailment",
    "full_grammar_and_style_review",
    "legal_copyright_opinion",
    "live_source_article_fetch",
)

ARTICLE_OUTPUT_CONTRACT = {
    "schema_version": ARTICLE_OUTPUT_SCHEMA_VERSION,
    "required_fields": list(REQUIRED_ARTICLE_FIELDS),
    "category_enum": sorted(ARTICLE_CATEGORIES),
    "claim_type_enum": sorted(CLAIM_TYPES),
    "quote_kind_enum": sorted(QUOTE_KINDS),
    "entity_type_enum": sorted(ENTITY_TYPES),
    "notes": (
        "evidence_refs and evidence_used may only cite URLs/sources from "
        "the article-generation evidence pack. Deterministic QA cannot "
        "semantically prove every claim is true."
    ),
    "future_qa_hooks": list(FUTURE_QA_HOOKS),
}
