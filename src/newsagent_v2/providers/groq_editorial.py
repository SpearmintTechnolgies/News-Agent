"""
Groq HTTPS adapter for the frozen editorial benchmark.

Uses the OpenAI-compatible Chat Completions API via `requests`.
Does not print, log, or persist API keys.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests

from newsagent_v2.benchmark.prompt import (
    build_editorial_messages,
    editorial_output_json_schema,
)
from newsagent_v2.benchmark.telemetry import redact_secrets

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_ID = "openai/gpt-oss-120b"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_TIMEOUT_SECONDS = 90
MAX_RETRY_AFTER_SECONDS = 15
TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
JSON_SCHEMA_NAME = "editorial_output_v1"
# Documented Groq model limits (not billed). Used for offline size checks only.
GROQ_CONTEXT_WINDOW = 131_072
GROQ_MAX_OUTPUT_TOKENS = 65_536
ERROR_MESSAGE_MAX_CHARS = 500

HttpPost = Callable[..., Any]


class GroqUnavailableError(RuntimeError):
    """Raised when Groq cannot be used (missing key, etc.). No HTTP is sent."""


class GroqHttpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def resolve_groq_api_key(environ: dict[str, str] | None = None) -> str | None:
    import os

    source = environ if environ is not None else os.environ
    raw = source.get("GROQ_API_KEY")
    if raw is None:
        return None
    key = str(raw).strip()
    return key or None


def _candidate_count(benchmark_input: dict[str, Any]) -> int:
    candidates = benchmark_input.get("candidates")
    if isinstance(candidates, list) and candidates:
        return len(candidates)
    count = benchmark_input.get("candidate_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
        return count
    raise ValueError("benchmark_input has no usable candidate count")


def build_chat_request_body(
    benchmark_input: dict[str, Any],
    *,
    model: str = GROQ_MODEL_ID,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    schema = editorial_output_json_schema(
        candidate_count=_candidate_count(benchmark_input),
    )
    return {
        "model": model,
        "messages": build_editorial_messages(benchmark_input),
        "reasoning_effort": reasoning_effort,
        "include_reasoning": False,
        "max_completion_tokens": 8192,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": JSON_SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            },
        },
    }


def request_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def public_request_metadata(
    body: dict[str, Any],
    *,
    url: str = GROQ_CHAT_COMPLETIONS_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return {
        "method": "POST",
        "url": url,
        "timeout_seconds": timeout_seconds,
        "body": body,
        "tools_enabled": False,
        "headers_included": False,
    }


def _default_http_post(
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
    timeout: int,
) -> requests.Response:
    return requests.post(url, headers=headers, json=json, timeout=timeout)


def _retry_after_seconds(headers: Any) -> float:
    if headers is None:
        return 1.0
    raw = None
    if hasattr(headers, "get"):
        raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return 1.0
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if wait < 0:
        return 0.0
    return min(wait, float(MAX_RETRY_AFTER_SECONDS))


def _status_code(response: Any) -> int:
    return int(getattr(response, "status_code"))


def _response_headers(response: Any) -> Any:
    return getattr(response, "headers", {}) or {}


def _response_json(response: Any) -> dict[str, Any]:
    if hasattr(response, "json"):
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise GroqHttpError("provider JSON was not an object")
    raise GroqHttpError("provider response has no json()")


def _provider_request_id(response: Any, payload: dict[str, Any] | None) -> str | None:
    headers = _response_headers(response)
    for key in ("x-request-id", "x-groq-request-id", "X-Request-Id"):
        if hasattr(headers, "get"):
            value = headers.get(key)
            if value:
                return str(value)
    if payload and isinstance(payload.get("id"), str):
        return payload["id"]
    return None


def parse_message_content(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise GroqHttpError("malformed provider response: missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise GroqHttpError("malformed provider response: choice")
    message = first.get("message")
    if not isinstance(message, dict):
        raise GroqHttpError("malformed provider response: message")
    if isinstance(message.get("parsed"), dict):
        return message["parsed"]
    content = message.get("content")
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise GroqHttpError("provider content was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise GroqHttpError("provider JSON was not an object")
        return parsed
    raise GroqHttpError("malformed provider response: content")


def extract_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
    def _int(name: str) -> int | None:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    return {
        "prompt_tokens": _int("prompt_tokens"),
        "completion_tokens": _int("completion_tokens"),
        "total_tokens": _int("total_tokens"),
    }


def estimate_prompt_tokens_from_bytes(size_bytes: int) -> int:
    """Offline token estimate: ~4 UTF-8 bytes per token. Not a provider count."""
    if size_bytes <= 0:
        return 0
    return (int(size_bytes) + 3) // 4


def safe_chat_request_diagnostics(body: dict[str, Any] | None) -> dict[str, Any]:
    """Size/shape of a chat request without storing prompt text or secrets."""
    body = body if isinstance(body, dict) else {}
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    parts: list[str] = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            parts.append(message["content"])
    prompt = "\n".join(parts)
    prompt_bytes = len(prompt.encode("utf-8"))
    serialized = json.dumps(body, ensure_ascii=False)
    serialized_bytes = len(serialized.encode("utf-8"))
    fmt = body.get("response_format") if isinstance(body.get("response_format"), dict) else {}
    json_schema = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else {}
    schema = json_schema.get("schema")
    schema_json = json.dumps(schema, ensure_ascii=False) if schema is not None else ""
    return {
        "model": body.get("model"),
        "max_completion_tokens": body.get("max_completion_tokens"),
        "response_format_type": fmt.get("type"),
        "json_schema_name": json_schema.get("name"),
        "strict": json_schema.get("strict"),
        "include_reasoning": body.get("include_reasoning"),
        "reasoning_effort": body.get("reasoning_effort"),
        "tools_enabled": bool(body.get("tools")),
        "message_count": len(messages),
        "prompt_characters": len(prompt),
        "prompt_bytes": prompt_bytes,
        "estimated_prompt_tokens": estimate_prompt_tokens_from_bytes(prompt_bytes),
        "serialized_characters": len(serialized),
        "serialized_bytes": serialized_bytes,
        "estimated_serialized_tokens": estimate_prompt_tokens_from_bytes(serialized_bytes),
        "schema_characters": len(schema_json),
        "model_context_window": GROQ_CONTEXT_WINDOW,
        "model_max_output_tokens": GROQ_MAX_OUTPUT_TOKENS,
        "estimated_admission_tokens": estimate_prompt_tokens_from_bytes(serialized_bytes),
        "admission_excludes_max_completion_tokens": True,
    }


def header_value(headers: Any, *names: str) -> str | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    for name in names:
        value = headers.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def sanitize_provider_error(
    *,
    status_code: int | None,
    payload: Any,
    headers: Any = None,
    secrets: list[str] | tuple[str, ...] = (),
    timestamp_utc: str | None = None,
    model: str | None = None,
    provider_request_id: str | None = None,
    request_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
    if not error and isinstance(payload, dict):
        error = {
            "message": payload.get("message"),
            "type": payload.get("type"),
            "code": payload.get("code"),
            "param": payload.get("param"),
        }
    message = error.get("message")
    if message is None:
        message = f"HTTP {status_code}" if status_code is not None else "provider_error"
    request_id = provider_request_id
    if request_id is None:
        dummy = type("Resp", (), {"headers": headers or {}})()
        request_id = _provider_request_id(
            dummy,
            payload if isinstance(payload, dict) else None,
        )
    record = {
        "http_status": status_code if isinstance(status_code, int) else None,
        "provider_error_code": error.get("code") or error.get("schema_code"),
        "provider_error_type": error.get("type"),
        "provider_error_message": str(message)[:ERROR_MESSAGE_MAX_CHARS],
        "provider_error_param": error.get("param"),
        "retry_after": header_value(headers, "Retry-After", "retry-after"),
        "provider_request_id": request_id,
        "timestamp_utc": timestamp_utc,
        "model": model,
        "request_diagnostics": request_diagnostics or {},
    }
    return redact_secrets(record, secrets)


def extract_provider_reported_cost_usd(payload: dict[str, Any]) -> float | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("cost", "total_cost", "cost_usd"):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def post_chat_completion(
    body: dict[str, Any],
    *,
    api_key: str | None,
    url: str = GROQ_CHAT_COMPLETIONS_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    http_post: HttpPost | None = None,
    sleep: Callable[[float], None] | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    if not api_key:
        raise GroqUnavailableError("GROQ_API_KEY is not set")

    poster = http_post or _default_http_post
    sleeper = sleep or (lambda _seconds: None)
    limit = int(max_attempts)
    if limit < 1:
        raise ValueError("max_attempts must be >= 1")
    headers = request_headers(api_key)
    attempts = 0
    last_status: int | None = None
    last_payload: dict[str, Any] | None = None
    last_response: Any = None
    retry_waits: list[float] = []
    last_retry_after: str | None = None

    while attempts < limit:
        attempts += 1
        response = poster(
            url,
            headers=headers,
            json=body,
            timeout=timeout_seconds,
        )
        last_response = response
        last_status = _status_code(response)
        retry_count = attempts - 1
        last_retry_after = header_value(_response_headers(response), "Retry-After", "retry-after")

        if last_status == 200:
            payload = _response_json(response)
            return {
                "ok": True,
                "status_code": last_status,
                "payload": payload,
                "retry_count": retry_count,
                "retry_after": last_retry_after,
                "retry_slept_seconds": sum(retry_waits),
                "attempts": attempts,
                "provider_request_id": _provider_request_id(response, payload),
                "headers": _response_headers(response),
            }

        if last_status in TRANSIENT_STATUS_CODES and attempts < limit:
            wait = _retry_after_seconds(_response_headers(response))
            retry_waits.append(wait)
            sleeper(wait)
            continue

        try:
            last_payload = _response_json(response)
        except GroqHttpError:
            last_payload = None
        return {
            "ok": False,
            "status_code": last_status,
            "payload": last_payload,
            "retry_count": retry_count,
            "retry_after": last_retry_after,
            "retry_slept_seconds": sum(retry_waits),
            "attempts": attempts,
            "provider_request_id": _provider_request_id(
                last_response,
                last_payload,
            ),
            "headers": _response_headers(response),
            "error": f"HTTP {last_status}",
        }

    return {
        "ok": False,
        "status_code": last_status,
        "payload": last_payload,
        "retry_count": 1,
        "retry_after": last_retry_after,
        "retry_slept_seconds": sum(retry_waits),
        "attempts": attempts,
        "provider_request_id": None,
        "headers": _response_headers(last_response) if last_response is not None else {},
        "error": f"HTTP {last_status}",
    }
