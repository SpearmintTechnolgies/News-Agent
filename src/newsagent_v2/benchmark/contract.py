"""
Provider-independent editorial output contract (editorial-output-v1).

This module is the source of truth for future Groq / Gemini / OpenRouter
(or any other) editorial providers. Every provider must emit the same
JSON shape so results can be compared without adapter-specific scoring.

The model may ONLY refer to candidate event IDs and evidence URLs that
appear in editorial_input.json. Invented IDs or evidence are invalid.
"""

from __future__ import annotations

EDITORIAL_INPUT_SCHEMA_VERSION = "editorial-input-v1"
EDITORIAL_OUTPUT_SCHEMA_VERSION = "editorial-output-v1"

BENCHMARK_CANDIDATE_LIMIT = 15
SELECTED_COUNT = 5

EVIDENCE_FIELDS = (
    "source",
    "source_type",
    "source_role",
    "source_authority",
    "title",
    "url",
    "published",
    "summary",
)

SEMANTIC_CATEGORIES = frozenset(
    {
        "regulatory",
        "security_incident",
        "market_move",
        "etf_product",
        "corporate",
        "political",
        "adoption",
        "exploit_hack",
        "legal",
        "macro",
        "other",
    }
)

SPECULATION_FLAGS = frozenset(
    {
        "none",
        "rumor",
        "unconfirmed",
        "forward_looking",
        "opinion_analysis",
        "historical_wording",
    }
)

REQUIRED_OUTPUT_FIELDS = (
    "schema_version",
    "selected_event_ids",
    "judgments",
)

REQUIRED_JUDGMENT_FIELDS = (
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
)

REQUIRED_SPECULATION_FIELDS = (
    "is_speculative",
    "flags",
)

EDITORIAL_OUTPUT_CONTRACT = {
    "schema_version": EDITORIAL_OUTPUT_SCHEMA_VERSION,
    "description": (
        "Structured editorial reasoning over a frozen Top-15 ranked "
        "event candidate set. Provider-independent."
    ),
    "constraints": {
        "selected_count": SELECTED_COUNT,
        "candidate_ids_must_exist": True,
        "evidence_must_exist_on_the_judged_event": True,
        "no_invented_ids": True,
        "no_invented_evidence": True,
    },
    "required_top_level_fields": list(REQUIRED_OUTPUT_FIELDS),
    "selected_event_ids": {
        "type": "array",
        "length": SELECTED_COUNT,
        "unique": True,
        "item_type": "string",
        "description": (
            "Exactly five publish-worthy candidate event IDs, in "
            "editorial priority order (1 = most publish-worthy)."
        ),
    },
    "judgments": {
        "type": "array",
        "description": (
            "One judgment object per benchmark candidate. Missing "
            "candidates are invalid."
        ),
        "item": {
            "event_id": {
                "type": "string",
                "must_be_candidate": True,
            },
            "selected": {
                "type": "boolean",
                "description": "True iff event_id is in selected_event_ids.",
            },
            "same_event_as": {
                "type": "array<string>",
                "unique": True,
                "symmetric": True,
                "description": (
                    "Other candidate event IDs judged to be the same "
                    "underlying event (duplicate / overlapping coverage). "
                    "Must not include self. Must not invent IDs. "
                    "IDs must be unique. Relations must be symmetric."
                ),
            },
            "is_current_event": {
                "type": "boolean",
                "description": (
                    "True if the candidate describes an actual current "
                    "event rather than historical background."
                ),
            },
            "is_background_context": {
                "type": "boolean",
                "description": (
                    "True if the headline/body is primarily historical "
                    "or contextual rather than the current action."
                ),
            },
            "semantic_category": {
                "type": "string",
                "enum": sorted(SEMANTIC_CATEGORIES),
            },
            "speculation": {
                "type": "object",
                "fields": {
                    "is_speculative": "boolean",
                    "flags": {
                        "type": "array<string>",
                        "enum": sorted(SPECULATION_FLAGS),
                    },
                },
            },
            "newsworthiness_reasoning": {
                "type": "string",
                "min_length": 1,
            },
            "evidence_refs": {
                "type": "array<object>",
                "item": {
                    "url": "string (must match an evidence URL on this event)",
                    "source": (
                        "optional; if present, must be a non-empty string "
                        "matching that evidence row"
                    ),
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "finite": True,
            },
            "rejection_reason": {
                "type": "string|null",
                "required_when_not_selected": True,
                "must_be_null_when_selected": True,
            },
        },
    },
    "deterministic_contradictions": [
        "selected True but event_id not in selected_event_ids",
        "selected False but event_id in selected_event_ids",
        "is_current_event and is_background_context both True",
        "duplicate same_event_as entries",
        "asymmetric same_event_as relation",
        "two selected events list each other in same_event_as",
        "evidence_refs.source present but not a non-empty string",
        "is_speculative False while flags contain a non-none flag",
        "is_speculative True while flags are empty or only 'none'",
        "flags contains 'none' together with any other flag",
    ],
}
