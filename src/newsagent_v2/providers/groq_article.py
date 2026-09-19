"""
Groq HTTPS adapter for one-pass article generation.

Reuses the editorial Chat Completions transport. Does not print, log,
or persist API keys. No tools, browsing, or search.
"""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.batch_prompt import (
    batch_output_json_schema,
    build_batch_messages,
    groq_batch_schema_keyword_paths,
)
from newsagent_v2.article.prompt import (
    article_output_json_schema,
    build_article_messages,
    groq_schema_keyword_paths,
)
from newsagent_v2.providers.groq_editorial import (
    DEFAULT_REASONING_EFFORT,
    GROQ_MODEL_ID,
)

JSON_SCHEMA_NAME = "article_output_v1"
BATCH_JSON_SCHEMA_NAME = "top5_article_batch_v1"
ARTICLE_MAX_COMPLETION_TOKENS = 8192
ARTICLE_TIMEOUT_SECONDS = 180
# Five 450–800 word articles plus compact claims/quotes/SEO JSON.
# Live #5 TPM 413 Requested=9778 matched serialized INPUT (~9977), not
# input+max_completion_tokens. Live #7 truncated at 10240 before required
# top-level `failures`. 16384 is bounded headroom for structured output.
BATCH_MAX_COMPLETION_TOKENS = 16384
BATCH_TIMEOUT_SECONDS = 300


class GroqSchemaCompatibilityError(RuntimeError):
    """Raised when the provider-facing schema contains a known-bad keyword."""


def build_article_chat_request_body(
    article_input: dict[str, Any],
    *,
    model: str = GROQ_MODEL_ID,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    schema = article_output_json_schema()
    bad_paths = groq_schema_keyword_paths(schema)
    if bad_paths:
        raise GroqSchemaCompatibilityError(
            "provider schema contains unsupported keywords: " + ", ".join(bad_paths)
        )
    return {
        "model": model,
        "messages": build_article_messages(article_input),
        "reasoning_effort": reasoning_effort,
        "include_reasoning": False,
        "max_completion_tokens": ARTICLE_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": JSON_SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            },
        },
    }


def build_top5_article_batch_request_body(
    *,
    batch_id: str,
    stories: list[dict[str, Any]],
    model: str = GROQ_MODEL_ID,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    schema = batch_output_json_schema()
    bad_paths = groq_batch_schema_keyword_paths(schema)
    if bad_paths:
        raise GroqSchemaCompatibilityError(
            "provider schema contains unsupported keywords: " + ", ".join(bad_paths)
        )
    return {
        "model": model,
        "messages": build_batch_messages(batch_id=batch_id, stories=stories),
        "reasoning_effort": reasoning_effort,
        "include_reasoning": False,
        "max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": BATCH_JSON_SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            },
        },
    }


# Re-export timeout default used by the article runner.
DEFAULT_ARTICLE_TIMEOUT_SECONDS = ARTICLE_TIMEOUT_SECONDS
