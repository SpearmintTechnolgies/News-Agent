"""One Groq request for all sufficient Top-5 stories. No per-story writer calls."""

from __future__ import annotations

import json
from typing import Any

from newsagent_v2.article.prompt import (
    SYSTEM_PROMPT,
    article_output_json_schema,
    groq_schema_keyword_paths,
)
from newsagent_v2.article.prompt_evidence import compact_story_evidence

BATCH_SYSTEM_PROMPT = """You are a crypto-news journalist writing for CoinNetwork.

You will receive one or more independently selected events in ONE request.
Write one complete newsroom article for each sufficient event, OR return an explicit failure for that event.
This is the only generation request. There is no second rewrite or repair call.

Do not browse the web. Do not use tools, search, or any external resource.
Do not invent facts, quotes, dates, amounts, people, causes, or consequences.

CROSS-STORY ISOLATION IS MANDATORY:
- Use only evidence attached to the current event_id.
- Never transfer facts, numbers, entities, quotes, dates, or context between event IDs.
- Never infer relationships between separate stories.
- Never mention another event_id inside an article.

Extracted source text is RESEARCH EVIDENCE ONLY. Do not copy source paragraphs.
Do not copy distinctive source sentences except short attributed direct quotes.
Paraphrase source facts in original prose.

EMIT ORDER:
- Emit the top-level `failures` array FIRST, then `articles`.
- Empty failures is valid: "failures": []
- Do not omit failures.

NORMAL ARTICLE LENGTH:
- HARD REQUIREMENT: Python-rendered article_body >= 350 words
- TARGET: 450-800 words
- Preferred generation target: roughly 500-650 words when evidence allows
- Emit 5-8 article_sections; paragraphs normally 60-110 words each
- Do not emit article_body; Python joins article_sections
- Never claim that length requirements are met unless the rendered word count meets them
- Do not pad with speculation, generic context, repetition, or unsupported background
- If evidence cannot safely support 350+ factual words, put the event in failures with an explicit reason

WRITER CONTRACT:
- claims[] must cover every factual paragraph via claim_ids
- EVERY factual paragraph in article_sections must list one or more claim_ids
- Every factual claim must have valid evidence_ids from that story's evidence_units
- Empty evidence_ids are forbidden
- Direct quotes must be in quotes[] with evidence_ids and a claim covering the attribution
- If a paragraph uses quotation marks, every quoted substring must appear EXACTLY as quotes[].text
- Do not glue two quoted remarks into one quotes[] entry
- If you cannot ledger the exact quoted substring, omit quotation marks and paraphrase safely
- Compound paragraphs may list multiple claim_ids
- Do not invent generic concluding analysis to sound finished
- A closing paragraph is not required
- Research evidence is provided as evidence_units with stable evidence_id, source, url, published, and text
- claims and quotes must cite those evidence_id values only; do not repeat url or source
- Do not emit evidence_used or generation_notes; Python derives them
- Do not expect extracted_text and factual_snippets; they are not duplicated in this payload

Every successful article must be article-output-v1 with grounded claims.
Return only the batch JSON object. Do not include internal reasoning.
""" + "\n\nSingle-article rules that still apply:\n" + SYSTEM_PROMPT


TOP1_WRITER_ADDENDUM = """
THIS REQUEST CONTAINS EXACTLY ONE STORY.

Produce one complete standalone news ARTICLE, not a news brief or summary.
Normally write 5-7 substantive paragraphs.
Each paragraph should normally contain enough supported detail that the Python-rendered article_body is approximately 500-650 words.
HARD minimum remains 350 words. TARGET remains 450-800 words.
DO NOT stop after a 180-250 word news brief.
Claim text must actually cover the assertions in the paragraph. Do not attach unrelated claim IDs.
Do not add unsupported filler to reach length.
If the evidence genuinely cannot support >=350 words, return a structured failure for this event_id instead of inventing material.
There is no repair call.
""".strip()


def batch_output_json_schema() -> dict[str, Any]:
    article = article_output_json_schema()
    failure_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["event_id", "code", "reason"],
        "properties": {
            "event_id": {"type": "string"},
            "code": {"type": "string"},
            "reason": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["failures", "articles"],
        "properties": {
            "failures": {
                "type": "array",
                "items": failure_item,
            },
            "articles": {
                "type": "array",
                "items": article,
            },
        },
    }


def build_batch_story_payload(row: dict[str, Any], *, rank: int) -> dict[str, Any]:
    compact = compact_story_evidence(row)
    return {
        "event_id": compact.get("event_id") or row.get("event_id"),
        "rank": rank,
        "representative_title": compact.get("representative_title"),
        "evidence_units": compact.get("evidence_units") or [],
        "evidence_metrics": compact.get("evidence_metrics") or {},
        "prompt_compaction": compact.get("prompt_compaction") or {},
    }


def build_batch_messages(
    *,
    batch_id: str,
    stories: list[dict[str, Any]],
) -> list[dict[str, str]]:
    payload = {
        "task": "Produce article-output-v1 for every requested event, or an explicit failure.",
        "batch_id": batch_id,
        "requested_event_ids": [row.get("event_id") for row in stories],
        "stories": [
            build_batch_story_payload(row, rank=index)
            for index, row in enumerate(stories, start=1)
        ],
    }
    system = BATCH_SYSTEM_PROMPT
    if len(stories) == 1:
        system = BATCH_SYSTEM_PROMPT + "\n\n" + TOP1_WRITER_ADDENDUM
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def groq_batch_schema_keyword_paths(schema: Any | None = None) -> list[str]:
    return groq_schema_keyword_paths(schema or batch_output_json_schema())
