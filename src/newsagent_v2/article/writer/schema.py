"""Article-first provider-native schemas.

Logical shape is shared. Groq adds additionalProperties:false (strict JSON
Schema). Gemini uses the documented generateContent Schema subset and must
not emit additionalProperties.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from newsagent_v2.article.contract import (
    ARTICLE_CATEGORIES,
    CLAIM_TYPES,
    ENTITY_TYPES,
    QUOTE_KINDS,
)

GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "uniqueItems",
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "definitions",
        "unevaluatedProperties",
        "patternProperties",
        "propertyNames",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "prefixItems",
        "contains",
        "minContains",
        "maxContains",
        "contentMediaType",
        "contentEncoding",
        "contentSchema",
    }
)

ARTICLE_FIRST_REQUIRED = (
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
    "claims",
    "paragraph_maps",
    "quotes",
)

LEDGER_FIRST_REQUIRED = (
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
)

CONTROLLED_V3_PROSE_REQUIRED = (
    "event_id",
    "headline",
    "dek",
    "paragraphs",
    "seo_title",
    "meta_description",
    "slug",
    "entities",
    "keywords",
)


def article_first_logical_schema() -> dict[str, Any]:
    """Provider-native article-first shape. No article_sections. No additionalProperties."""
    categories = sorted(ARTICLE_CATEGORIES)
    claim_types = sorted(CLAIM_TYPES)
    quote_kinds = sorted(QUOTE_KINDS)
    entity_types = sorted(ENTITY_TYPES)
    string_array = {"type": "array", "items": {"type": "string"}, "minItems": 1}
    return {
        "type": "object",
        "required": list(ARTICLE_FIRST_REQUIRED),
        "properties": {
            "event_id": {"type": "string"},
            "headline": {"type": "string"},
            "dek": {"type": "string"},
            "article_body": {"type": "string"},
            "category": {"type": "string", "enum": categories},
            "seo_title": {"type": "string"},
            "meta_description": {"type": "string"},
            "slug": {"type": "string"},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "type"],
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": entity_types},
                    },
                },
            },
            "keywords": {"type": "array", "items": {"type": "string"}},
            "claims": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["id", "text", "claim_type", "evidence_ids"],
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "claim_type": {"type": "string", "enum": claim_types},
                        "evidence_ids": string_array,
                    },
                },
            },
            "paragraph_maps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["paragraph_index", "claim_ids"],
                    "properties": {
                        "paragraph_index": {"type": "integer"},
                        "claim_ids": string_array,
                    },
                },
            },
            "quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["text", "kind", "attribution", "evidence_ids"],
                    "properties": {
                        "text": {"type": "string"},
                        "kind": {"type": "string", "enum": quote_kinds},
                        "attribution": {"type": "string"},
                        "evidence_ids": string_array,
                    },
                },
            },
        },
    }


def ledger_first_logical_schema() -> dict[str, Any]:
    """Provider-native prose fields only. Claims/quotes are attached after generation."""
    schema = article_first_logical_schema()
    properties = dict(schema.get("properties") or {})
    properties.pop("claims", None)
    properties.pop("paragraph_maps", None)
    properties.pop("quotes", None)
    return {
        "type": "object",
        "required": list(LEDGER_FIRST_REQUIRED),
        "properties": {key: properties[key] for key in LEDGER_FIRST_REQUIRED if key in properties},
    }


def _with_additional_properties_false(node: Any) -> Any:
    if isinstance(node, dict):
        out = {key: _with_additional_properties_false(value) for key, value in node.items()}
        if out.get("type") == "object":
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_with_additional_properties_false(item) for item in node]
    return node


def groq_article_first_json_schema() -> dict[str, Any]:
    """Strict JSON Schema for Groq json_schema. additionalProperties is allowed here."""
    return _with_additional_properties_false(article_first_logical_schema())


def groq_ledger_first_json_schema() -> dict[str, Any]:
    """Strict Groq JSON Schema for ledger-first prose. No claims/quotes/paragraph_maps."""
    return _with_additional_properties_false(ledger_first_logical_schema())


def controlled_v3_prose_logical_schema() -> dict[str, Any]:
    """Isolated paragraph outputs. Provider must not emit a claim ledger."""
    entity_types = sorted(ENTITY_TYPES)
    return {
        "type": "object",
        "required": list(CONTROLLED_V3_PROSE_REQUIRED),
        "properties": {
            "event_id": {"type": "string"},
            "headline": {"type": "string"},
            "dek": {"type": "string"},
            "paragraphs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["paragraph_id", "sentences"],
                    "properties": {
                        "paragraph_id": {"type": "string"},
                        "sentences": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["sentence_id", "text", "fact_ids_used", "quote_ids_used"],
                                "properties": {
                                    "sentence_id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "fact_ids_used": {"type": "array", "items": {"type": "string"}},
                                    "quote_ids_used": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                    },
                },
            },
            "seo_title": {"type": "string"},
            "meta_description": {"type": "string"},
            "slug": {"type": "string"},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "type"],
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": entity_types},
                    },
                },
            },
            "keywords": {"type": "array", "items": {"type": "string"}},
        },
    }


def groq_controlled_v3_json_schema() -> dict[str, Any]:
    return _with_additional_properties_false(controlled_v3_prose_logical_schema())


def gemini_article_first_schema() -> dict[str, Any]:
    """generateContent responseSchema subset. Must not contain additionalProperties."""
    return _to_gemini_schema(article_first_logical_schema())


def gemini_ledger_first_schema() -> dict[str, Any]:
    """generateContent schema for ledger-first prose. No claims/quotes/paragraph_maps."""
    return _to_gemini_schema(ledger_first_logical_schema())


def _to_gemini_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_to_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in GEMINI_UNSUPPORTED_SCHEMA_KEYS:
            continue
        out[key] = _to_gemini_schema(value)
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        props = out["properties"]
        required = out.get("required")
        if isinstance(required, list) and required:
            ordering = [name for name in required if name in props]
            ordering.extend(name for name in props if name not in ordering)
            out["propertyOrdering"] = ordering
    return out


def schema_contains_additional_properties(node: Any) -> bool:
    if isinstance(node, dict):
        if "additionalProperties" in node:
            return True
        return any(schema_contains_additional_properties(value) for value in node.values())
    if isinstance(node, list):
        return any(schema_contains_additional_properties(item) for item in node)
    return False


def gemini_schema_violations(node: Any, *, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in GEMINI_UNSUPPORTED_SCHEMA_KEYS:
                found.append(f"{path}.{key}")
            found.extend(gemini_schema_violations(value, path=f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(gemini_schema_violations(value, path=f"{path}[{index}]"))
    return found


def failing_gemini_schema_from_groq_article_contract() -> dict[str, Any]:
    """The shape that caused HTTP 400: Groq JSON Schema sent as responseSchema."""
    return deepcopy(groq_article_first_json_schema())
