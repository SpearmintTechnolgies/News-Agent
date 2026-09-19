"""OpenAI-compatible Qwen vLLM writer adapter. No Groq/Gemini. No QA changes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from newsagent_v2.article.writer.prompts import ARTICLE_FIRST_SYSTEM_PROMPT
from newsagent_v2.article.writer.protocol import FrozenStoryPackage, ProviderWriterResult
from newsagent_v2.article.writer.schema import groq_article_first_json_schema
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS

KEY_ENV = "NEWSAGENT_V2_QWEN_API_KEY"
BASE_ENV = "NEWSAGENT_V2_QWEN_BASE_URL"
QWEN_MODEL = "vllm-local/qwen3.8-27b"
PROVIDER_NAME = "qwen_vllm"
DEFAULT_TIMEOUT_SECONDS = 360
MODELS_TIMEOUT_SECONDS = 30
ERROR_MESSAGE_MAX_CHARS = 500
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
_HOST_RE = re.compile(r"host=['\"][^'\"]+['\"]", re.IGNORECASE)
_RESOLVE_RE = re.compile(r"Failed to resolve '[^']+'")
_TUNNEL_RE = re.compile(r"[A-Za-z0-9.-]+\.trycloudflare\.com", re.IGNORECASE)

Transport = Callable[..., Any]


class QwenVLLMConfigError(ValueError):
    """Missing or invalid Qwen vLLM configuration. Never includes secret values."""


@dataclass(frozen=True)
class QwenVLLMConfig:
    api_key: str
    base_url: str
    model: str = QWEN_MODEL

    def __post_init__(self) -> None:
        key = self.api_key.strip()
        base = self.base_url.strip().rstrip("/")
        if not key:
            raise QwenVLLMConfigError(f"{KEY_ENV} is empty")
        if not base:
            raise QwenVLLMConfigError(f"{BASE_ENV} is empty")
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise QwenVLLMConfigError(f"{BASE_ENV} is not a usable URL")
        object.__setattr__(self, "api_key", key)
        object.__setattr__(self, "base_url", base)

    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,)


def load_qwen_config(environ: dict[str, str] | None) -> QwenVLLMConfig:
    if environ is None:
        raise QwenVLLMConfigError("environ must be provided explicitly")
    key = environ.get(KEY_ENV)
    base = environ.get(BASE_ENV)
    if key is None:
        raise QwenVLLMConfigError(f"{KEY_ENV} is not set")
    if base is None:
        raise QwenVLLMConfigError(f"{BASE_ENV} is not set")
    return QwenVLLMConfig(api_key=str(key), base_url=str(base), model=QWEN_MODEL)


def credential_presence(environ: dict[str, str]) -> dict[str, bool]:
    return {
        "qwen_api_key_present": bool(str(environ.get(KEY_ENV) or "").strip()),
        "qwen_base_url_present": bool(str(environ.get(BASE_ENV) or "").strip()),
    }


def sanitize_error(message: str, secrets: tuple[str, ...] = ()) -> str:
    text = str(message or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _URL_RE.sub("[redacted-url]", text)
    text = _HOST_RE.sub("host=[redacted-host]", text)
    text = _RESOLVE_RE.sub("Failed to resolve [redacted-host]", text)
    text = _TUNNEL_RE.sub("[redacted-host]", text)
    return text[:ERROR_MESSAGE_MAX_CHARS]


def api_join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _headers(config: QwenVLLMConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def default_transport(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    timeout: int,
) -> requests.Response:
    return requests.request(
        method,
        url,
        headers=headers,
        json=json_body,
        timeout=timeout,
        allow_redirects=False,
    )


def _status_code(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    return int(value) if isinstance(value, int) else None


def _payload(response: Any) -> dict[str, Any] | None:
    try:
        data = response.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _error_from_payload(payload: dict[str, Any] | None, status: int | None) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err.strip():
            return err
        if payload.get("detail"):
            return str(payload["detail"])
    return f"HTTP {status}"


def model_ids_from_payload(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        if isinstance(payload.get("id"), str):
            return [payload["id"]]
        return []
    ids: list[str] = []
    for row in data:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip():
            ids.append(row["id"].strip())
        elif isinstance(row, str) and row.strip():
            ids.append(row.strip())
    return ids


def thinking_controls_advertised(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    blob = json.dumps(payload, ensure_ascii=False).lower()
    return "enable_thinking" in blob or "chat_template_kwargs" in blob


def inspect_models(
    config: QwenVLLMConfig,
    *,
    transport: Transport | None = None,
    timeout_seconds: int = MODELS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    poster = transport or default_transport
    url = api_join(config.base_url, "models")
    try:
        response = poster(
            url,
            method="GET",
            headers=_headers(config),
            json_body=None,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "http_status": None,
            "requested_model": QWEN_MODEL,
            "requested_model_available": False,
            "model_ids": [],
            "thinking_controls_advertised": False,
            "error": sanitize_error(str(exc), config.secrets()),
        }
    status = _status_code(response)
    payload = _payload(response)
    ids = model_ids_from_payload(payload)
    available = QWEN_MODEL in ids
    ok = status == 200 and available
    error = None if ok else sanitize_error(_error_from_payload(payload, status), config.secrets())
    if status == 200 and not available:
        error = "requested model unavailable"
    return {
        "ok": ok,
        "http_status": status,
        "requested_model": QWEN_MODEL,
        "requested_model_available": available,
        "model_ids_count": len(ids),
        "thinking_controls_advertised": thinking_controls_advertised(payload),
        "error": error,
    }


def post_chat_completion(
    config: QwenVLLMConfig,
    body: dict[str, Any],
    *,
    transport: Transport | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    poster = transport or default_transport
    url = api_join(config.base_url, "chat/completions")
    try:
        response = poster(
            url,
            method="POST",
            headers=_headers(config),
            json_body=body,
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "http_status": None,
            "payload": None,
            "error": sanitize_error(str(exc), config.secrets()),
            "retry_count": 0,
        }
    status = _status_code(response)
    payload = _payload(response)
    if status != 200 or payload is None:
        return {
            "ok": False,
            "http_status": status,
            "payload": redact_secrets(payload, config.secrets()) if payload is not None else None,
            "error": sanitize_error(_error_from_payload(payload, status), config.secrets()),
            "retry_count": 0,
        }
    return {
        "ok": True,
        "http_status": status,
        "payload": payload,
        "error": None,
        "retry_count": 0,
    }


def _message_content(payload: dict[str, Any]) -> str | dict[str, Any] | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    parsed = message.get("parsed")
    if isinstance(parsed, dict):
        return parsed
    content = message.get("content")
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        return content
    return None


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = _THINK_RE.sub("", raw).strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("provider content was not valid JSON")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("provider JSON was not an object")
    return parsed


def parse_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    content = _message_content(payload)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("malformed provider response: content")
    return _extract_json_object(content)


def extract_usage(payload: dict[str, Any] | None) -> dict[str, int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    usage = payload["usage"]

    def _int(name: str, *alts: str) -> int | None:
        for key in (name, *alts):
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            return int(value)
        return None

    prompt = _int("prompt_tokens", "input_tokens")
    completion = _int("completion_tokens", "output_tokens")
    total = _int("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def strip_reasoning_from_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    cleaned = json.loads(json.dumps(payload))
    choices = cleaned.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                message.pop("reasoning_content", None)
                message.pop("reasoning", None)
                content = message.get("content")
                if isinstance(content, str):
                    message["content"] = _THINK_RE.sub("", content).strip()
    return cleaned


class QwenVLLMWriterProvider:
    provider_name = PROVIDER_NAME
    model_name = QWEN_MODEL

    def __init__(self, config: QwenVLLMConfig, *, enable_thinking: bool | None = None) -> None:
        self._config = config
        self.enable_thinking = enable_thinking

    def secrets(self) -> tuple[str, ...]:
        return self._config.secrets()

    def build_request(
        self,
        story: FrozenStoryPackage,
        *,
        compact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_id = story.event_id
        frozen = compact if isinstance(compact, dict) else {}
        payload = {
            "task": "Write one complete article_body first, then grounding maps for the same body.",
            "batch_id": "writer-bakeoff",
            "event_id": frozen.get("event_id") or event_id,
            "evidence_units": frozen.get("evidence_units") or [],
            "evidence_metrics": frozen.get("evidence_metrics") or {},
            "requirements": {
                "hard_minimum_words": 350,
                "target_min_words": 450,
                "target_max_words": 800,
                "preferred_min_words": 450,
                "preferred_max_words": 600,
                "repair_call": False,
            },
        }
        body: dict[str, Any] = {
            "model": QWEN_MODEL,
            "messages": [
                {"role": "system", "content": ARTICLE_FIRST_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0,
            "max_tokens": BATCH_MAX_COMPLETION_TOKENS,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "article_first_v1",
                    "strict": True,
                    "schema": groq_article_first_json_schema(),
                },
            },
        }
        if self.enable_thinking is False:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def parse_response(self, payload: dict[str, Any]) -> ProviderWriterResult:
        try:
            native = parse_chat_payload(payload)
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            return ProviderWriterResult(
                ok=False,
                provider=PROVIDER_NAME,
                model=QWEN_MODEL,
                native=None,
                error=sanitize_error(str(exc), self.secrets()),
            )
        return ProviderWriterResult(
            ok=True,
            provider=PROVIDER_NAME,
            model=QWEN_MODEL,
            native=native,
            error=None,
        )

    def generate(
        self,
        story: FrozenStoryPackage,
        *,
        compact: dict[str, Any] | None = None,
        transport: Transport | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        body = self.build_request(story, compact=compact)
        http = post_chat_completion(
            self._config,
            body,
            transport=transport,
            timeout_seconds=timeout_seconds,
        )
        parsed = None
        if http.get("ok") and isinstance(http.get("payload"), dict):
            parsed = self.parse_response(http["payload"])
            parsed.http_status = http.get("http_status") if isinstance(http.get("http_status"), int) else None
        return {
            "http": http,
            "parsed": parsed,
            "request": body,
        }
