"""Bedrock Mantle WriterProvider for Kimi K2.5. Real HTTP is blocked until authorized."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from newsagent_v2.article.writer.prompts import ARTICLE_FIRST_SYSTEM_PROMPT
from newsagent_v2.article.writer.protocol import FrozenStoryPackage, ProviderWriterResult
from newsagent_v2.article.writer.schema import groq_article_first_json_schema
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS

KEY_ENV = "NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY"
BASE_URL = "https://bedrock-mantle.us-east-1.api.aws/v1"
KIMI_MODEL = "moonshotai.kimi-k2.5"
PROVIDER_NAME = "bedrock_mantle"
DEFAULT_TIMEOUT_SECONDS = 180
ERROR_MESSAGE_MAX_CHARS = 500
HARD_MAX_GENERATION_CALLS = 1
HTTP_RETRY_TOTAL = 0
PROVIDER_RETRY_TOTAL = 0
QUALITY_RETRY_TOTAL = 0
REPAIR_CALL_TOTAL = 0
FALLBACK_CALL_TOTAL = 0
# Permanent default. One real call requires the explicit one-shot token, then latches closed.
REAL_INFERENCE_AUTHORIZED = False
ONE_SHOT_AUTHORIZATION_TOKEN = "EXACTLY_ONE_REAL_KIMI_K2_5_EVENT_005"
_ONE_SHOT_CONSUMED = False

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)

Transport = Callable[..., Any]

FATAL_HTTP_STATUSES = frozenset({401, 403, 404, 408, 429})


class BedrockMantleConfigError(ValueError):
    """Missing or invalid Bedrock Mantle configuration. Never includes secret values."""


class KimiRealHttpBlockedError(RuntimeError):
    """Raised when real Bedrock Mantle HTTP is attempted before inference is authorized."""


class KimiCallCapError(RuntimeError):
    """Raised when a second real/mocked generation call would exceed the hard cap."""


class KimiStoppedError(RuntimeError):
    """Raised when a prior provider/API failure already stopped the controlled run."""


@dataclass(frozen=True)
class BedrockMantleConfig:
    api_key: str
    base_url: str = BASE_URL
    model: str = KIMI_MODEL

    def __post_init__(self) -> None:
        key = self.api_key.strip()
        base = self.base_url.strip().rstrip("/")
        if not key:
            raise BedrockMantleConfigError(f"{KEY_ENV} is empty")
        if base != BASE_URL.rstrip("/"):
            raise BedrockMantleConfigError("Bedrock Mantle base URL is not the approved endpoint")
        object.__setattr__(self, "api_key", key)
        object.__setattr__(self, "base_url", base)

    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,)


def load_bedrock_mantle_config(environ: dict[str, str] | None) -> BedrockMantleConfig:
    if environ is None:
        raise BedrockMantleConfigError("environ must be provided explicitly")
    key = environ.get(KEY_ENV)
    if key is None:
        raise BedrockMantleConfigError(f"{KEY_ENV} is not set")
    return BedrockMantleConfig(api_key=str(key), base_url=BASE_URL, model=KIMI_MODEL)


def credential_presence(environ: dict[str, str]) -> dict[str, bool]:
    return {
        "bedrock_mantle_api_key_present": bool(str(environ.get(KEY_ENV) or "").strip()),
    }


def sanitize_error(message: str, secrets: tuple[str, ...] = ()) -> str:
    text = str(message or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", text)
    text = _URL_RE.sub("[redacted-url]", text)
    return text[:ERROR_MESSAGE_MAX_CHARS]


def api_join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def chat_completions_url() -> str:
    return api_join(BASE_URL, "chat/completions")


def _headers(config: BedrockMantleConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def no_retry_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=HTTP_RETRY_TOTAL, connect=0, read=0, redirect=0, status=0, other=0)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def default_transport(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
    timeout: int,
) -> requests.Response:
    del url, method, headers, json_body, timeout
    raise KimiRealHttpBlockedError(
        "real Bedrock Mantle HTTP is blocked until Kimi inference is authorized"
    )


def one_shot_remaining() -> bool:
    return not _ONE_SHOT_CONSUMED


def consume_one_shot_authorization(token: str) -> bool:
    """Latch closed after the first accepted token. Never logs the credential."""
    global _ONE_SHOT_CONSUMED
    if token != ONE_SHOT_AUTHORIZATION_TOKEN:
        return False
    if _ONE_SHOT_CONSUMED:
        return False
    _ONE_SHOT_CONSUMED = True
    return True


def build_one_shot_real_transport(config: BedrockMantleConfig) -> Transport:
    """HTTPS POST with retries disabled. Authorization is used here and never returned."""
    used = {"n": 0}
    session = no_retry_session()

    def transport(
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        timeout: int,
    ) -> requests.Response:
        del headers
        if used["n"] >= 1:
            raise KimiCallCapError("hard maximum real Kimi generation calls is 1")
        used["n"] += 1
        return session.request(
            method,
            url,
            headers=_headers(config),
            json=json_body,
            timeout=timeout,
            allow_redirects=False,
        )

    return transport


def _status_code(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _payload(response: Any) -> dict[str, Any] | None:
    reader = getattr(response, "json", None)
    if not callable(reader):
        return None
    try:
        payload = reader()
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _error_from_payload(payload: dict[str, Any] | None, status: int | None) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error.strip():
            return error
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
    if status is None:
        return "provider request failed"
    return f"HTTP {status}"


def is_fatal_provider_failure(*, http_status: int | None, error_class: str | None) -> bool:
    if error_class in {
        "timeout",
        "connection_error",
        "schema_incompatibility",
        "unexpected_response",
        "http_error",
        "call_cap",
        "stopped",
        "real_http_blocked",
    }:
        return True
    if http_status is None:
        return bool(error_class)
    if http_status in FATAL_HTTP_STATUSES:
        return True
    if http_status >= 500:
        return True
    return http_status != 200


def classify_http_failure(exc: BaseException | None, status: int | None) -> str:
    if isinstance(exc, (requests.Timeout, requests.ConnectTimeout, requests.ReadTimeout)):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection_error"
    if isinstance(exc, KimiRealHttpBlockedError):
        return "real_http_blocked"
    if isinstance(exc, KimiCallCapError):
        return "call_cap"
    if isinstance(exc, KimiStoppedError):
        return "stopped"
    if status is None:
        return "unexpected_response"
    if status in FATAL_HTTP_STATUSES or status >= 500 or status != 200:
        return "http_error"
    return "unexpected_response"


class KimiCallLedger:
    """Hard cap of one generation transport invocation. No retries or fallbacks."""

    def __init__(self) -> None:
        self.generation_calls = 0
        self.retry_count = 0
        self.repair_calls = 0
        self.fallback_calls = 0
        self.quality_retries = 0
        self.stopped = False
        self.stop_reason: str | None = None
        self.last_http_status: int | None = None

    def note_transport_call(self) -> None:
        if self.stopped:
            raise KimiStoppedError(self.stop_reason or "Kimi run already stopped")
        if self.generation_calls >= HARD_MAX_GENERATION_CALLS:
            self.stopped = True
            self.stop_reason = "hard generation call cap reached"
            raise KimiCallCapError("hard maximum real Kimi generation calls is 1")
        self.generation_calls += 1

    def stop(self, reason: str, *, http_status: int | None = None) -> None:
        self.stopped = True
        self.stop_reason = reason
        if http_status is not None:
            self.last_http_status = http_status


def post_chat_completion(
    config: BedrockMantleConfig,
    body: dict[str, Any],
    *,
    ledger: KimiCallLedger,
    transport: Transport | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    authorize_one_real_call: str | None = None,
) -> dict[str, Any]:
    secrets = config.secrets()
    if PROVIDER_RETRY_TOTAL != 0 or HTTP_RETRY_TOTAL != 0:
        ledger.stop("retries are forbidden")
        return {
            "ok": False,
            "http_status": None,
            "payload": None,
            "error": "retries are forbidden",
            "error_class": "unexpected_response",
            "retry_count": 0,
            "real_http_attempted": False,
        }

    poster = transport
    real_http_attempted = False
    if poster is None:
        if authorize_one_real_call is not None:
            if authorize_one_real_call != ONE_SHOT_AUTHORIZATION_TOKEN:
                ledger.stop("real Kimi HTTP is not authorized")
                return {
                    "ok": False,
                    "http_status": None,
                    "payload": None,
                    "error": "real Kimi HTTP is not authorized",
                    "error_class": "real_http_blocked",
                    "retry_count": 0,
                    "real_http_attempted": False,
                }
            if not consume_one_shot_authorization(authorize_one_real_call):
                ledger.stop("Kimi one-shot already consumed")
                return {
                    "ok": False,
                    "http_status": None,
                    "payload": None,
                    "error": "Kimi one-shot already consumed",
                    "error_class": "call_cap",
                    "retry_count": 0,
                    "real_http_attempted": False,
                }
            poster = build_one_shot_real_transport(config)
            real_http_attempted = True
        elif not REAL_INFERENCE_AUTHORIZED:
            ledger.stop("real Kimi HTTP is not authorized")
            return {
                "ok": False,
                "http_status": None,
                "payload": None,
                "error": "real Kimi HTTP is not authorized",
                "error_class": "real_http_blocked",
                "retry_count": 0,
                "real_http_attempted": False,
            }
        else:
            poster = default_transport
            real_http_attempted = True

    try:
        ledger.note_transport_call()
    except (KimiCallCapError, KimiStoppedError) as exc:
        return {
            "ok": False,
            "http_status": ledger.last_http_status,
            "payload": None,
            "error": sanitize_error(str(exc), secrets),
            "error_class": classify_http_failure(exc, None),
            "retry_count": 0,
            "real_http_attempted": False,
        }

    url = chat_completions_url()
    try:
        response = poster(
            url,
            method="POST",
            headers={"Content-Type": "application/json"},
            json_body=body,
            timeout=timeout_seconds,
        )
    except (requests.Timeout, requests.ConnectTimeout, requests.ReadTimeout) as exc:
        ledger.stop("timeout", http_status=None)
        return {
            "ok": False,
            "http_status": None,
            "payload": None,
            "error": sanitize_error(str(exc), secrets),
            "error_class": "timeout",
            "retry_count": 0,
            "real_http_attempted": real_http_attempted,
        }
    except requests.ConnectionError as exc:
        ledger.stop("connection_error", http_status=None)
        return {
            "ok": False,
            "http_status": None,
            "payload": None,
            "error": sanitize_error(str(exc), secrets),
            "error_class": "connection_error",
            "retry_count": 0,
            "real_http_attempted": real_http_attempted,
        }
    except KimiRealHttpBlockedError as exc:
        ledger.stop("real_http_blocked")
        ledger.generation_calls = max(0, ledger.generation_calls - 1)
        return {
            "ok": False,
            "http_status": None,
            "payload": None,
            "error": sanitize_error(str(exc), secrets),
            "error_class": "real_http_blocked",
            "retry_count": 0,
            "real_http_attempted": False,
        }
    except requests.RequestException as exc:
        ledger.stop("unexpected_response")
        return {
            "ok": False,
            "http_status": None,
            "payload": None,
            "error": sanitize_error(str(exc), secrets),
            "error_class": "unexpected_response",
            "retry_count": 0,
            "real_http_attempted": real_http_attempted,
        }

    status = _status_code(response)
    payload = _payload(response)
    if status != 200 or payload is None:
        error_class = "schema_incompatibility" if status == 200 and payload is None else classify_http_failure(None, status)
        if status == 200 and payload is None:
            error_class = "schema_incompatibility"
        ledger.stop(error_class, http_status=status)
        return {
            "ok": False,
            "http_status": status,
            "payload": redact_secrets(payload, secrets) if payload is not None else None,
            "error": sanitize_error(_error_from_payload(payload, status), secrets),
            "error_class": error_class,
            "retry_count": 0,
            "real_http_attempted": real_http_attempted,
        }
    return {
        "ok": True,
        "http_status": status,
        "payload": payload,
        "error": None,
        "error_class": None,
        "retry_count": 0,
        "real_http_attempted": real_http_attempted,
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
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str) and part.strip():
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                texts.append(part["text"])
        if texts:
            return "\n".join(texts)
    return None


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = _THINK_RE.sub("", raw).strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError as exc:
        raise ValueError("provider content was not valid JSON") from exc
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


class BedrockMantleKimiWriterProvider:
    provider_name = PROVIDER_NAME
    model_name = KIMI_MODEL

    def __init__(self, config: BedrockMantleConfig, *, ledger: KimiCallLedger | None = None) -> None:
        self._config = config
        self.ledger = ledger if ledger is not None else KimiCallLedger()

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
                "fallback_model": None,
                "quality_retry": False,
            },
        }
        return {
            "model": KIMI_MODEL,
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

    def parse_response(self, payload: dict[str, Any]) -> ProviderWriterResult:
        try:
            native = parse_chat_payload(payload)
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            return ProviderWriterResult(
                ok=False,
                provider=PROVIDER_NAME,
                model=KIMI_MODEL,
                native=None,
                error=sanitize_error(str(exc), self.secrets()),
            )
        return ProviderWriterResult(
            ok=True,
            provider=PROVIDER_NAME,
            model=KIMI_MODEL,
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
        authorize_one_real_call: str | None = None,
    ) -> dict[str, Any]:
        body = self.build_request(story, compact=compact)
        http = post_chat_completion(
            self._config,
            body,
            ledger=self.ledger,
            transport=transport,
            timeout_seconds=timeout_seconds,
            authorize_one_real_call=authorize_one_real_call,
        )
        parsed = None
        if http.get("ok") and isinstance(http.get("payload"), dict):
            parsed = self.parse_response(http["payload"])
            parsed.http_status = http.get("http_status") if isinstance(http.get("http_status"), int) else None
            if not parsed.ok:
                self.ledger.stop("schema_incompatibility", http_status=parsed.http_status)
                http = {
                    **http,
                    "ok": False,
                    "error": parsed.error,
                    "error_class": "schema_incompatibility",
                }
        return {
            "http": http,
            "parsed": parsed,
            "request": body,
            "ledger": {
                "generation_calls": self.ledger.generation_calls,
                "retry_count": self.ledger.retry_count,
                "repair_calls": self.ledger.repair_calls,
                "fallback_calls": self.ledger.fallback_calls,
                "quality_retries": self.ledger.quality_retries,
                "stopped": self.ledger.stopped,
                "stop_reason": self.ledger.stop_reason,
            },
        }
