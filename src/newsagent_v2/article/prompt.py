"""
CoinNetwork article-generation prompt and Groq-facing JSON Schema.

Provider schema omits keywords already known to be unsupported by Groq
(uniqueItems). Local QA still enforces uniqueness and evidence rules.
Compact Groq output: claims/quotes cite evidence_ids; Python resolves
url/source and renders article_body.
"""

from __future__ import annotations

import json
from typing import Any

from newsagent_v2.article.contract import (
    ARTICLE_CATEGORIES,
    ARTICLE_OUTPUT_SCHEMA_VERSION,
    CLAIM_TYPES,
    ENTITY_TYPES,
    QUOTE_KINDS,
)

UNSUPPORTED_GROQ_SCHEMA_KEYWORDS = ("uniqueItems",)

SYSTEM_PROMPT = """You are a crypto-news journalist writing for CoinNetwork.

Write one complete newsroom article from the frozen article-input only.

Do not browse the web.
Do not use tools, search, code execution, or any external resource.
Do not invent facts, quotes, dates, amounts, people, causes, or consequences.
Do not infer confirmation from speculation, rumor, or investigator comment.
If the evidence does not support a statement, omit it.

Use only supplied evidence_id values. Do not invent evidence IDs, source IDs, or URLs.
Do not emit source names or URLs on claims or quotes; Python already has that metadata.

Distinguish clearly in prose and in structured claims:
- confirmed fact
- attribution (who said what)
- allegation
- speculation
- contextual information

Write natural professional newsroom prose.
Avoid AI filler, generic introductions, phrases like "In the rapidly evolving..." or "It remains to be seen...", repetitive conclusions, unnecessary headings, fake quotations, sensationalism, unsupported causal claims, and keyword stuffing.
Do not imitate or closely paraphrase source titles or summaries. Synthesize independently.

Headline: describe the supported event, concise, no clickbait, no unsupported certainty, no numbers unless they appear in the supplied evidence.

Direct quotes only if the exact wording appears in the supplied evidence. Otherwise paraphrase or omit quotes.
If you use quotation marks in a paragraph, quotes[].text must be the EXACT quoted substring from that paragraph.
Do not glue two quotes into one ledger entry. If you cannot ledger the exact substring, omit quotation marks and paraphrase safely.
Every factual, number, date, attribution, and quote claim in `claims` must cite evidence_ids from this story's evidence_units. Empty evidence_ids are forbidden.

The claims array is the evidence ledger for body units, not a subset of source facts.
Do not emit article_body. Emit article_sections with paragraphs that each list claim_ids.
Python will join those paragraphs into the final article_body.
EVERY factual paragraph must include one or more claim_ids that exist in claims[].
Every claim_id referenced in a paragraph must exist. Every claim must have evidence_ids.
Compound sentences in a paragraph may list multiple claim_ids covering the union of assertions.

Do not invent generic concluding analysis to make the article sound finished.
A concluding paragraph is not required. End on the last supported factual development.
Do not write unsupported lines such as "This development underscores...", "The move highlights...", "Stakeholders will be watching...", "could reshape the regulatory landscape...", "demonstrates the growing...", or similar generic industry conclusions unless those exact assertions are evidenced and claimed.

Do NOT state that something was not disclosed, was not announced, was not confirmed, is unknown, remains unknown, was unavailable, has not happened, or was absent merely because the supplied evidence does not mention it.
Missing evidence is not evidence of absence.

Do not add generic filler or industry-trend context merely to increase article length.
Extracted source text in the evidence pack is RESEARCH EVIDENCE ONLY.
Do not copy source paragraphs. Do not paste extracted_text into article_body.
Do not copy distinctive source prose sentence-for-sentence. Paraphrase in original newsroom language.
The only allowed near-copying is a short attributed direct quote whose exact wording appears in evidence.
Names, bill titles, organizations, dates, and numbers may recur; surrounding prose must still be rewritten.

NORMAL ARTICLE LENGTH — HARD REQUIREMENT: the Python-rendered article_body must be at least 350 words.
TARGET: 450-800 words. Preferred generation target is roughly 500-650 words when evidence allows.
Emit 5 to 8 article_sections. Each section has one or more paragraphs.
Each paragraph should normally be about 60-110 words of grounded newsroom prose and must list claim_ids.
Never claim that length requirements are met unless the rendered word count meets them.
Do not pad with speculation, generic context, repetition, or unsupported background.
If evidence is sufficient for a factual article but cannot safely support 350+ words without filler, do not invent prose: return an explicit structured failure for that event instead.

Do not invent industry trends.
Do not invent causal interpretation.
Do not fabricate quotes.
Return grounded content only.

SEO title, meta description, and slug must describe this article naturally.
Return only article-output-v1. Do not include internal reasoning.
"""


def groq_schema_keyword_paths(
    schema: Any,
    *,
    keywords: tuple[str, ...] = UNSUPPORTED_GROQ_SCHEMA_KEYWORDS,
) -> list[str]:
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for keyword in keywords:
                if keyword in node:
                    found.append(f"{path}.{keyword}")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(schema, "$")
    return found


def _id_list_schema(*, min_items: int = 1) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": min_items,
        "items": {"type": "string"},
    }


def article_output_json_schema() -> dict[str, Any]:
    categories = sorted(ARTICLE_CATEGORIES)
    claim_types = sorted(CLAIM_TYPES)
    quote_kinds = sorted(QUOTE_KINDS)
    entity_types = sorted(ENTITY_TYPES)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "event_id",
            "headline",
            "dek",
            "article_sections",
            "category",
            "seo_title",
            "meta_description",
            "slug",
            "entities",
            "keywords",
            "claims",
            "quotes",
        ],
        "properties": {
            # schema_version, article_body, evidence_used, and generation_notes
            # are stamped/derived in Python. Groq must not emit them.
            "event_id": {"type": "string"},
            "headline": {"type": "string"},
            "dek": {"type": "string"},
            "category": {
                "type": "string",
                "enum": categories,
            },
            "seo_title": {"type": "string"},
            "meta_description": {"type": "string"},
            "slug": {"type": "string"},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "type"],
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": entity_types,
                        },
                    },
                },
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
            },
            "claims": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "text", "claim_type", "evidence_ids"],
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "claim_type": {
                            "type": "string",
                            "enum": claim_types,
                        },
                        "evidence_ids": _id_list_schema(min_items=1),
                    },
                },
            },
            "quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "kind", "attribution", "evidence_ids"],
                    "properties": {
                        "text": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": quote_kinds,
                        },
                        "attribution": {"type": "string"},
                        "evidence_ids": _id_list_schema(min_items=1),
                    },
                },
            },
            "article_sections": {
                "type": "array",
                "minItems": 5,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "paragraphs"],
                    "properties": {
                        "id": {"type": "string"},
                        "paragraphs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["text", "claim_ids"],
                                "properties": {
                                    "text": {"type": "string"},
                                    "claim_ids": _id_list_schema(min_items=1),
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def build_article_messages(article_input: dict[str, Any]) -> list[dict[str, str]]:
    user_payload = {
        "task": "Produce article-output-v1 for this frozen selected event.",
        "output_schema_version": ARTICLE_OUTPUT_SCHEMA_VERSION,
        "article_input": article_input,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]
