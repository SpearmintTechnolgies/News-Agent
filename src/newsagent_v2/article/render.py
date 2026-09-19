"""Render article_body from structured article_sections. No LLM."""

from __future__ import annotations

from typing import Any

SECTION_JOIN = "\n\n"
PARAGRAPH_JOIN = "\n\n"


def iter_section_paragraphs(article: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sections = article.get("article_sections")
    if not isinstance(sections, list):
        return rows
    for section in sections:
        if not isinstance(section, dict):
            continue
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        for paragraph in paragraphs:
            if isinstance(paragraph, dict):
                rows.append(paragraph)
    return rows


def render_article_body(article: dict[str, Any]) -> str:
    sections = article.get("article_sections")
    if not isinstance(sections, list) or not sections:
        body = article.get("article_body")
        return body.strip() if isinstance(body, str) else ""
    blocks: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        texts = [
            str(paragraph.get("text") or "").strip()
            for paragraph in paragraphs
            if isinstance(paragraph, dict) and str(paragraph.get("text") or "").strip()
        ]
        if texts:
            blocks.append(PARAGRAPH_JOIN.join(texts))
    return SECTION_JOIN.join(blocks).strip()


def materialize_article(article: Any) -> Any:
    """Stamp rendered article_body when article_sections are present."""
    if not isinstance(article, dict):
        return article
    if isinstance(article.get("article_sections"), list) and article["article_sections"]:
        article["article_body"] = render_article_body(article)
    return article
