"""Bake-off prompts. Article-first uses the writer adapter. Structured is legacy."""

from __future__ import annotations

from newsagent_v2.article.batch_prompt import build_batch_messages
from newsagent_v2.article.prompt import groq_schema_keyword_paths
from newsagent_v2.article.writer.prompts import ARTICLE_FIRST_SYSTEM_PROMPT, article_first_messages
from newsagent_v2.article.writer.schema import groq_article_first_json_schema

__all__ = [
    "ARTICLE_FIRST_SYSTEM_PROMPT",
    "article_first_messages",
    "article_first_json_schema",
    "structured_messages",
    "groq_article_first_schema_ok",
]


def article_first_json_schema():
    return groq_article_first_json_schema()


def structured_messages(*, batch_id: str, story: dict) -> list[dict[str, str]]:
    return build_batch_messages(batch_id=batch_id, stories=[story])


def groq_article_first_schema_ok() -> list[str]:
    return groq_schema_keyword_paths(groq_article_first_json_schema())
