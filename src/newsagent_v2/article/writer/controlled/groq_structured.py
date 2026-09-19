"""Groq structured-output root-object contract. Provider boundary only."""

from __future__ import annotations

import json
import re
from typing import Any

from newsagent_v2.article.writer.schema import groq_controlled_v3_json_schema

ROOT_OBJECT_CONTRACT = """
GROQ STRUCTURED OUTPUT SHAPE:
RETURN EXACTLY ONE JSON OBJECT AT THE ROOT.
The first non-whitespace character MUST be {
The final non-whitespace character MUST be }
DO NOT wrap the object in [].
DO NOT return a list of articles.
DO NOT return an array containing one object.
DO NOT return Markdown fences.
DO NOT return commentary before or after the object.
There is exactly ONE article result.
Arrays are allowed ONLY for schema-defined array fields:
paragraphs[], sentences[], fact_ids_used[], quote_ids_used[], entities[], keywords[].
The ROOT is NEVER an array.
""".strip()

ROOT_ARRAY_FORBIDDEN = True


class GroqStructuredOutputError(ValueError):
    """Invalid Groq structured payload. Not a QA or editorial failure."""


def _walk_required_matches_properties(node: Any, path: str = "$") -> list[str]:
    issues: list[str] = []
    if not isinstance(node, dict):
        return issues
    if node.get("type") == "object" and isinstance(node.get("properties"), dict):
        props = set(node["properties"])
        required = node.get("required")
        if not isinstance(required, list) or set(required) != props:
            issues.append(f"{path}: required must equal properties")
        if node.get("additionalProperties") is not False:
            issues.append(f"{path}: additionalProperties must be false")
        for name, child in node["properties"].items():
            issues.extend(_walk_required_matches_properties(child, f"{path}.{name}"))
    items = node.get("items")
    if isinstance(items, dict):
        issues.extend(_walk_required_matches_properties(items, f"{path}[]"))
    return issues


def assert_groq_root_object_schema(schema: dict[str, Any] | None = None) -> dict[str, Any]:
    schema = schema if schema is not None else groq_controlled_v3_json_schema()
    if schema.get("type") != "object":
        raise GroqStructuredOutputError("root schema must be type=object")
    issues = _walk_required_matches_properties(schema)
    if issues:
        raise GroqStructuredOutputError("; ".join(issues))
    paragraphs = ((schema.get("properties") or {}).get("paragraphs") or {}).get("items") or {}
    if "text" in (paragraphs.get("properties") or {}):
        raise GroqStructuredOutputError("obsolete paragraph-level text is present")
    return {
        "root_schema_type": "object",
        "root_array_allowed": False,
        "schema_valid": True,
    }


def with_root_object_system_message(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    if not messages:
        return [{"role": "system", "content": ROOT_OBJECT_CONTRACT}]
    first = dict(messages[0])
    content = str(first.get("content") or "")
    if ROOT_OBJECT_CONTRACT not in content:
        first["content"] = ROOT_OBJECT_CONTRACT + "\n\n" + content
    return [first, *messages[1:]]


def decode_strict_root_object(raw: Any, *, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Accept only a single JSON object. Never unwrap [object]. Never salvage failed_generation."""
    schema = schema if schema is not None else groq_controlled_v3_json_schema()
    if isinstance(raw, list):
        raise GroqStructuredOutputError("root JSON array is forbidden")
    if isinstance(raw, dict):
        native = raw
    elif isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("```"):
            raise GroqStructuredOutputError("Markdown fenced JSON is forbidden")
        if not stripped.startswith("{") or not stripped.endswith("}"):
            raise GroqStructuredOutputError("root must be a JSON object")
        try:
            native = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise GroqStructuredOutputError("malformed JSON") from exc
        if isinstance(native, list):
            raise GroqStructuredOutputError("root JSON array is forbidden")
        if not isinstance(native, dict):
            raise GroqStructuredOutputError("root JSON was not an object")
    else:
        raise GroqStructuredOutputError("root JSON was not an object")
    props = set((schema.get("properties") or {}).keys())
    required = list(schema.get("required") or [])
    extra = set(native) - props
    if extra:
        raise GroqStructuredOutputError(f"unknown root properties: {sorted(extra)}")
    missing = [key for key in required if key not in native]
    if missing:
        raise GroqStructuredOutputError(f"missing required root properties: {missing}")
    return native


def refuse_failed_generation(payload: dict[str, Any] | None) -> None:
    """Explicit non-salvage: failed_generation must not become native JSON."""
    del payload


def sanitized_request_snapshot(request_body: dict[str, Any]) -> dict[str, Any]:
    schema = ((request_body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
    messages = request_body.get("messages") if isinstance(request_body.get("messages"), list) else []
    blob = json.dumps(messages)
    system = str((messages[0] or {}).get("content") or "") if messages else ""
    return {
        "model": request_body.get("model"),
        "root_schema_type": schema.get("type"),
        "root_array_allowed": False,
        "prompt_requires_one_root_object": "EXACTLY ONE JSON OBJECT" in blob,
        "root_array_example_present": bool(
            re.search(r"example[^\n]*\[", system, re.I) or re.search(r"```(?:json)?\s*\[", system, re.I)
        ),
        "schema_preflight": assert_groq_root_object_schema(schema),
        "messages": messages,
        "response_format": request_body.get("response_format"),
        "secrets_present": False,
    }
