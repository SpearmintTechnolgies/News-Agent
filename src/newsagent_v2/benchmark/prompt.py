"""
Batched CoinNetwork editorial prompt and JSON Schema for Groq structured output.

Derived from frozen editorial-output-v1. Does not change validator semantics.
"""

from __future__ import annotations

import json
from typing import Any

from .contract import (
    EDITORIAL_OUTPUT_SCHEMA_VERSION,
    SELECTED_COUNT,
    SEMANTIC_CATEGORIES,
    SPECULATION_FLAGS,
)

SYSTEM_PROMPT = """You are the editorial decision engine for CoinNetwork, a crypto newsroom.

You receive a frozen set of ranked event candidates plus their evidence.
Reason ONLY over that supplied evidence.

Do not browse the web.
Do not use tools, search, code execution, or any external resource.
Do not invent facts.
Do not invent candidate event IDs.
Do not invent evidence URLs or sources.
Do not claim evidence that is not in the supplied members.

Evaluate every supplied candidate.
Select exactly 5 publish-worthy events.
Resolve same-event / duplicate coverage between candidate IDs (relations must be symmetric).
Do not select two candidates that represent the same underlying event.

same_event_as means two candidates describe the SAME underlying news event.
Do NOT use same_event_as merely because stories involve the same company, project, or person; arise from the same exploit; are part of the same broader incident; are follow-up developments; or share background context.
Example: "exploit occurs" and "network resumes after exploit" can be separate news events.
Example: "network resumes" and "company refuses ransom related to exploit" can also be separate developments unless the evidence shows they describe the same underlying event.
If A lists B in same_event_as, B must list A.
Distinguish the current action from background or historical wording (for example, a shutdown described "after launch" is about shutdown, not a new launch).
Assign a semantic event category from the allowed enum.
Identify speculation or uncertainty using only the allowed flags.
Prefer concrete, timely, consequential, well-supported crypto stories.

Deterministic ranking scores and dimension breakdowns are supporting metadata only.
Do not blindly follow them. You may disagree when the evidence warrants it.

Return exactly editorial-output-v1 structured JSON.
newsworthiness_reasoning must be a short editorial justification for publication or rejection.
Do not reveal hidden chain-of-thought or private scratchpad reasoning.
For selected events, rejection_reason must be null.
For non-selected events, rejection_reason must be a non-empty string.
evidence_refs must cite only supplied evidence URLs.
Include source as the exact source string from that evidence row.
"""


def editorial_output_json_schema(*, candidate_count: int) -> dict[str, Any]:
    if not isinstance(candidate_count, int) or isinstance(candidate_count, bool):
        raise TypeError("candidate_count must be an int")
    if candidate_count < 1:
        raise ValueError("candidate_count must be >= 1")
    categories = sorted(SEMANTIC_CATEGORIES)
    flags = sorted(SPECULATION_FLAGS)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "selected_event_ids",
            "judgments",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": EDITORIAL_OUTPUT_SCHEMA_VERSION,
            },
            "selected_event_ids": {
                "type": "array",
                "minItems": SELECTED_COUNT,
                "maxItems": SELECTED_COUNT,
                "items": {"type": "string"},
            },
            "judgments": {
                "type": "array",
                "minItems": candidate_count,
                "maxItems": candidate_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "event_id",
                        "selected",
                        "same_event_as",
                        "is_current_event",
                        "is_background_context",
                        "semantic_category",
                        "speculation",
                        "newsworthiness_reasoning",
                        "evidence_refs",
                        "confidence",
                        "rejection_reason",
                    ],
                    "properties": {
                        "event_id": {"type": "string"},
                        "selected": {"type": "boolean"},
                        "same_event_as": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "is_current_event": {"type": "boolean"},
                        "is_background_context": {"type": "boolean"},
                        "semantic_category": {
                            "type": "string",
                            "enum": categories,
                        },
                        "speculation": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["is_speculative", "flags"],
                            "properties": {
                                "is_speculative": {"type": "boolean"},
                                "flags": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": flags,
                                    },
                                },
                            },
                        },
                        "newsworthiness_reasoning": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "evidence_refs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["url", "source"],
                                "properties": {
                                    "url": {"type": "string"},
                                    "source": {"type": "string"},
                                },
                            },
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "rejection_reason": {
                            "type": ["string", "null"],
                        },
                    },
                },
            },
        },
    }


def build_editorial_messages(benchmark_input: dict[str, Any]) -> list[dict[str, str]]:
    user_payload = {
        "task": "Produce editorial-output-v1 for this frozen benchmark input.",
        "output_schema_version": EDITORIAL_OUTPUT_SCHEMA_VERSION,
        "selected_count_required": SELECTED_COUNT,
        "benchmark_input": benchmark_input,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]
