"""Offline Gemini request schema validation. No HTTP."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.writer.schema import gemini_schema_violations, schema_contains_additional_properties


def validate_gemini_response_schema(schema: Any) -> dict[str, Any]:
    violations = gemini_schema_violations(schema)
    return {
        "ok": not violations,
        "has_additionalProperties": schema_contains_additional_properties(schema),
        "violations": violations,
    }


def validate_gemini_generate_content_body(body: dict[str, Any]) -> dict[str, Any]:
    config = body.get("generationConfig") if isinstance(body.get("generationConfig"), dict) else {}
    schema = config.get("responseSchema")
    result = validate_gemini_response_schema(schema)
    result["responseMimeType"] = config.get("responseMimeType")
    result["has_responseSchema"] = schema is not None
    return result
