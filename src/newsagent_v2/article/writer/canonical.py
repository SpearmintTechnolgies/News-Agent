"""CanonicalArticle: provider-independent internal article-output-v1.

QA consumes this shape only. Provider JSON may use different field names.
"""

from __future__ import annotations

from newsagent_v2.article.contract import (
    ARTICLE_OUTPUT_SCHEMA_VERSION,
    REQUIRED_ARTICLE_FIELDS,
    REQUIRED_CLAIM_FIELDS,
    REQUIRED_QUOTE_FIELDS,
)

CANONICAL_SCHEMA_VERSION = ARTICLE_OUTPUT_SCHEMA_VERSION

CANONICAL_ARTICLE_FIELDS = REQUIRED_ARTICLE_FIELDS + ("article_sections",)

CANONICAL_CLAIM_FIELDS = REQUIRED_CLAIM_FIELDS
CANONICAL_QUOTE_FIELDS = REQUIRED_QUOTE_FIELDS

CANONICAL_PARAGRAPH_MAP_FIELDS = ("paragraph_index", "claim_ids")

PROVIDER_CLAIM_ID_ALIASES = ("claim_id", "id")
