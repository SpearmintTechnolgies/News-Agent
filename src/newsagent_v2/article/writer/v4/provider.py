"""V4 writer provider selection and OpenAI-compatible transports.

Pipeline code stays provider-agnostic. Selection is env/config only.
Kimi and paid private Qwen adapters exist but stay idle unless explicitly allowed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from newsagent_v2.providers.groq_editorial import (
    GROQ_CHAT_COMPLETIONS_URL,
    GroqHttpError,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    request_headers as groq_request_headers,
    resolve_groq_api_key,
)

# --- env names (no secret values) ---
ENV_PROVIDER = "NEWSAGENT_V2_V4_WRITER_PROVIDER"
ENV_MODEL = "NEWSAGENT_V2_V4_WRITER_MODEL"
ENV_FALLBACK_PROVIDER = "NEWSAGENT_V2_V4_WRITER_FALLBACK_PROVIDER"
ENV_FALLBACK_MODEL = "NEWSAGENT_V2_V4_WRITER_FALLBACK_MODEL"
ENV_ALLOW_PAID_QWEN = "NEWSAGENT_V2_V4_ALLOW_PAID_QWEN"
ENV_ALLOW_KIMI = "NEWSAGENT_V2_V4_ALLOW_KIMI"
ENV_KIMI_BASE_URL = "NEWSAGENT_V2_V4_KIMI_BASE_URL"
ENV_MAX_PROVIDER_ATTEMPTS = "NEWSAGENT_V2_V4_MAX_PROVIDER_ATTEMPTS"

PROVIDER_GROQ = "groq"
PROVIDER_QWEN = "qwen_vllm"
PROVIDER_KIMI = "kimi"

DEFAULT_PROVIDER = PROVIDER_GROQ
DEFAULT_MODEL = "qwen/qwen3.8-27b"
DEFAULT_FALLBACK_PROVIDER = PROVIDER_GROQ
DEFAULT_FALLBACK_MODEL = "openai/gpt-oss-20b"
DEFAULT_MAX_PROVIDER_ATTEMPTS = 2

# Groq on_demand OTPM for qwen/qwen3.8-27b observed live as 1000.
# Requested max_completion_tokens must stay safely below this or Groq 429s
# before generation (event-027: Requested 1076 > Limit 1000).
KNOWN_GROQ_QWEN_OTPM_LIMIT = 1000
V4_MAX_COMPLETION_TOKENS = 900  # must remain < KNOWN_GROQ_QWEN_OTPM_LIMIT
assert V4_MAX_COMPLETION_TOKENS < KNOWN_GROQ_QWEN_OTPM_LIMIT
# Mantle/Kimi budget hard max is 1500; use it so RICH drafts can clear the
# production 600-word QA floor (Groq's 900-token cap does not apply here).
KIMI_V4_MAX_COMPLETION_TOKENS = 1500

# Same-org Groq failover cannot reset OTPM/ITPM; sleep+retry is the real recovery.
GROQ_RATE_LIMIT_RETRIES = 2
GROQ_RATE_LIMIT_WAIT_MIN_S = 5.0
GROQ_RATE_LIMIT_WAIT_MAX_S = 60.0


def _parse_retry_after_seconds(value: Any) -> float | None:
    """Parse Retry-After / provider wait hint into seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = re.search(r"(\d+(?:\.\d+)?)\s*s", text, flags=re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None

# Infrastructure error classes â€” content QA failures must NOT failover.
INFRA_RATE_LIMIT = "RATE_LIMIT"
INFRA_TIMEOUT = "TIMEOUT"
INFRA_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
INFRA_SERVER_ERROR = "SERVER_ERROR"
INFRA_AUTH = "AUTH"
INFRA_OTHER = "OTHER"

INFRA_FAILOVER_TYPES = frozenset(
    {
        INFRA_RATE_LIMIT,
        INFRA_TIMEOUT,
        INFRA_MODEL_UNAVAILABLE,
        INFRA_SERVER_ERROR,
    }
)


@dataclass(frozen=True)
class V4ProviderSpec:
    provider: str
    model: str
    chat_url: str
    api_key_env: str
    api_key_present: bool
    allowed: bool
    notes: str = ""


@dataclass
class ChatCompletionResult:
    ok: bool
    payload: dict[str, Any] | None = None
    content: Any = None
    status_code: int | None = None
    error: str | None = None
    error_type: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    attempts: int = 1
    finish_reason: str | None = None
    retry_after: str | None = None
    rate_limit_headers: dict[str, str] = field(default_factory=dict)
    request_shape: dict[str, Any] = field(default_factory=dict)


def reasoning_effort_for_model(model: str) -> str | None:
    """Model-specific reasoning knobs. Qwen prose: none. GPT-OSS: low."""
    lowered = str(model or "").strip().lower()
    if "qwen" in lowered:
        return "none"
    if "gpt-oss" in lowered or "openai/gpt-oss" in lowered:
        return "low"
    return None


def build_v4_chat_body(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_completion_tokens: int = V4_MAX_COMPLETION_TOKENS,
    temperature: float = 0.3,
    body_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical Groq chat body. No Qwen-only params on GPT-OSS."""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
    }
    effort = reasoning_effort_for_model(model)
    if effort is not None:
        body["reasoning_effort"] = effort
    if body_extra:
        extra = dict(body_extra)
        # Never let callers override model-specific reasoning with incompatible values.
        if "reasoning_effort" in extra and effort is not None:
            extra["reasoning_effort"] = effort
        body.update(extra)
    return body


def extract_rate_limit_telemetry(headers: Any, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Capture Retry-After and Groq rate-limit headers without secrets."""
    out: dict[str, str] = {}
    retry_after = None
    if headers is not None and hasattr(headers, "get"):
        for key in (
            "Retry-After",
            "retry-after",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-tokens",
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
        ):
            value = headers.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if key.lower() in {"retry-after"}:
                retry_after = text
            out[key.lower()] = text
    finish_reason = None
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
    return {
        "retry_after": retry_after,
        "rate_limit_headers": out,
        "finish_reason": str(finish_reason) if finish_reason else None,
    }


def same_org_groq_failover_is_otpm_safe(
    *,
    primary_provider: str,
    fallback_provider: str,
    error_type: str | None,
) -> bool:
    """Same-org Groqâ†’Groq does not reset organization OTPM on RATE_LIMIT."""
    if str(error_type or "") != INFRA_RATE_LIMIT:
        return True
    return not (
        primary_provider == PROVIDER_GROQ and fallback_provider == PROVIDER_GROQ
    )


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def classify_provider_error(
    message: str,
    *,
    status_code: int | None = None,
) -> str:
    text = (message or "").lower()
    code = int(status_code or 0)
    if code == 401 or code == 403 or "unauthorized" in text or "invalid api key" in text:
        return INFRA_AUTH
    if (
        code == 429
        or "rate limit" in text
        or "otpm" in text
        or "tokens per minute" in text
        or "request too large for model" in text
        and "per minute" in text
    ):
        return INFRA_RATE_LIMIT
    if code == 408 or "timeout" in text or "timed out" in text:
        return INFRA_TIMEOUT
    if code == 404 or "model_not_found" in text or "does not exist" in text or "unavailable" in text:
        return INFRA_MODEL_UNAVAILABLE
    if code >= 500 or "server error" in text or "bad gateway" in text:
        return INFRA_SERVER_ERROR
    return INFRA_OTHER


def is_infrastructure_failure(error_type: str | None) -> bool:
    return str(error_type or "") in INFRA_FAILOVER_TYPES


def resolve_v4_provider_specs(environ: dict[str, str] | None) -> dict[str, Any]:
    """Describe configured adapters without reading secret values into logs."""
    env = dict(environ or {})
    from newsagent_v2.article.writer.qwen_vllm import BASE_ENV as QWEN_BASE_ENV
    from newsagent_v2.article.writer.qwen_vllm import KEY_ENV as QWEN_KEY_ENV
    from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV

    groq_key = bool(str(resolve_groq_api_key(env) or "").strip())
    qwen_key = bool(str(env.get(QWEN_KEY_ENV) or "").strip())
    qwen_base = bool(str(env.get(QWEN_BASE_ENV) or "").strip())
    kimi_key = bool(str(env.get(KIMI_KEY_ENV) or "").strip())
    allow_paid = _truthy(env.get(ENV_ALLOW_PAID_QWEN))
    allow_kimi = _truthy(env.get(ENV_ALLOW_KIMI))

    primary = V4ProviderSpec(
        provider=str(env.get(ENV_PROVIDER) or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER,
        model=str(env.get(ENV_MODEL) or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        chat_url=GROQ_CHAT_COMPLETIONS_URL,
        api_key_env="GROQ_API_KEY",
        api_key_present=groq_key,
        allowed=True,
        notes="free Groq OpenAI-compatible chat",
    )
    if primary.provider == PROVIDER_QWEN:
        primary = V4ProviderSpec(
            provider=PROVIDER_QWEN,
            model=str(env.get(ENV_MODEL) or "vllm-local/qwen3.8-27b").strip(),
            chat_url="",  # filled by adapter from base URL
            api_key_env=QWEN_KEY_ENV,
            api_key_present=qwen_key and qwen_base,
            allowed=allow_paid,
            notes="paid private Qwen vLLM; idle unless ALLOW_PAID_QWEN=1",
        )
    elif primary.provider == PROVIDER_KIMI:
        primary = V4ProviderSpec(
            provider=PROVIDER_KIMI,
            model=str(env.get(ENV_MODEL) or "moonshotai.kimi-k3").strip(),
            chat_url="",
            api_key_env=KIMI_KEY_ENV,
            api_key_present=kimi_key,
            allowed=allow_kimi,
            notes="Kimi; idle unless ALLOW_KIMI=1",
        )

    fb_provider = str(env.get(ENV_FALLBACK_PROVIDER) or DEFAULT_FALLBACK_PROVIDER).strip().lower()
    fb_model = str(env.get(ENV_FALLBACK_MODEL) or DEFAULT_FALLBACK_MODEL).strip()
    fallback = V4ProviderSpec(
        provider=fb_provider or DEFAULT_FALLBACK_PROVIDER,
        model=fb_model or DEFAULT_FALLBACK_MODEL,
        chat_url=GROQ_CHAT_COMPLETIONS_URL if (fb_provider or DEFAULT_FALLBACK_PROVIDER) == PROVIDER_GROQ else "",
        api_key_env="GROQ_API_KEY" if (fb_provider or DEFAULT_FALLBACK_PROVIDER) == PROVIDER_GROQ else QWEN_KEY_ENV,
        api_key_present=groq_key
        if (fb_provider or DEFAULT_FALLBACK_PROVIDER) == PROVIDER_GROQ
        else (qwen_key and qwen_base),
        allowed=(fb_provider or DEFAULT_FALLBACK_PROVIDER) == PROVIDER_GROQ
        or ((fb_provider == PROVIDER_QWEN) and allow_paid)
        or ((fb_provider == PROVIDER_KIMI) and allow_kimi),
        notes="bounded infra failover only",
    )
    try:
        max_attempts = max(1, int(str(env.get(ENV_MAX_PROVIDER_ATTEMPTS) or DEFAULT_MAX_PROVIDER_ATTEMPTS)))
    except ValueError:
        max_attempts = DEFAULT_MAX_PROVIDER_ATTEMPTS

    return {
        "primary": primary,
        "fallback": fallback,
        "max_provider_attempts": max_attempts,
        "credentials": {
            "groq_api_key_present": groq_key,
            "qwen_api_key_present": qwen_key,
            "qwen_base_url_present": qwen_base,
            "kimi_api_key_present": kimi_key,
        },
        "allow_paid_qwen": allow_paid,
        "allow_kimi": allow_kimi,
        "available_adapters": [PROVIDER_GROQ, PROVIDER_QWEN, PROVIDER_KIMI],
        "recommended_next_free_provider": PROVIDER_GROQ,
        "recommended_next_free_model": DEFAULT_FALLBACK_MODEL,
        "activation_env_names": [
            ENV_PROVIDER,
            ENV_MODEL,
            ENV_FALLBACK_PROVIDER,
            ENV_FALLBACK_MODEL,
            ENV_ALLOW_PAID_QWEN,
            ENV_ALLOW_KIMI,
            ENV_MAX_PROVIDER_ATTEMPTS,
            "GROQ_API_KEY",
            QWEN_KEY_ENV,
            QWEN_BASE_ENV,
            KIMI_KEY_ENV,
        ],
    }


class ChatTransport:
    """Provider-native chat completion. Never logs secrets."""

    provider_name: str
    model: str

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        body_extra: dict[str, Any] | None = None,
        max_completion_tokens: int = V4_MAX_COMPLETION_TOKENS,
        temperature: float = 0.3,
        event_id: str | None = None,
        stage: str | None = None,
    ) -> ChatCompletionResult:
        raise NotImplementedError


@dataclass
class GroqChatTransport(ChatTransport):
    api_key: str
    model: str
    http_post: Callable[..., Any] | None = None
    timeout_seconds: int = 120
    provider_name: str = PROVIDER_GROQ
    chat_url: str = GROQ_CHAT_COMPLETIONS_URL

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        body_extra: dict[str, Any] | None = None,
        max_completion_tokens: int = V4_MAX_COMPLETION_TOKENS,
        temperature: float = 0.3,
        event_id: str | None = None,
        stage: str | None = None,
    ) -> ChatCompletionResult:
        # event_id/stage are accepted for writer parity with Kimi; unused on Groq.
        _ = (event_id, stage)
        last: ChatCompletionResult | None = None
        for attempt in range(GROQ_RATE_LIMIT_RETRIES + 1):
            last = self._complete_once(
                messages=messages,
                body_extra=body_extra,
                max_completion_tokens=max_completion_tokens,
                temperature=temperature,
            )
            if last.ok or last.error_type != INFRA_RATE_LIMIT:
                return last
            if attempt >= GROQ_RATE_LIMIT_RETRIES:
                return last
            wait_s = _parse_retry_after_seconds(last.retry_after)
            if wait_s is None:
                # Also mine wait hint from error text ("try again in 33.1s").
                wait_s = _parse_retry_after_seconds(last.error)
            wait_s = max(GROQ_RATE_LIMIT_WAIT_MIN_S, min(float(wait_s or 35.0), GROQ_RATE_LIMIT_WAIT_MAX_S))
            time.sleep(wait_s)
        assert last is not None
        return last

    def _complete_once(
        self,
        *,
        messages: list[dict[str, str]],
        body_extra: dict[str, Any] | None = None,
        max_completion_tokens: int = V4_MAX_COMPLETION_TOKENS,
        temperature: float = 0.3,
    ) -> ChatCompletionResult:
        body = build_v4_chat_body(
            model=self.model,
            messages=messages,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            body_extra=body_extra,
        )
        request_shape = {
            "model": self.model,
            "has_response_format": "response_format" in body,
            "reasoning_effort": body.get("reasoning_effort"),
            "max_completion_tokens": body.get("max_completion_tokens"),
            "keys": sorted(body.keys()),
        }
        try:
            if self.http_post is not None:
                response = self.http_post(
                    self.chat_url,
                    headers=groq_request_headers(self.api_key or "test-key"),
                    json=body,
                    timeout=self.timeout_seconds,
                )
                status = getattr(response, "status_code", None)
                payload = response.json() if hasattr(response, "json") else response
                headers = getattr(response, "headers", {}) or {}
                telem = extract_rate_limit_telemetry(
                    headers, payload if isinstance(payload, dict) else None
                )
                if status and int(status) >= 400:
                    message = f"HTTP {status}"
                    if isinstance(payload, dict):
                        err = payload.get("error")
                        if isinstance(err, dict) and err.get("message"):
                            message = str(err["message"])
                    return ChatCompletionResult(
                        ok=False,
                        payload=payload if isinstance(payload, dict) else None,
                        status_code=int(status),
                        error=message[:400],
                        error_type=classify_provider_error(message, status_code=int(status)),
                        provider=self.provider_name,
                        model=self.model,
                        retry_after=telem.get("retry_after"),
                        rate_limit_headers=dict(telem.get("rate_limit_headers") or {}),
                        finish_reason=telem.get("finish_reason"),
                        request_shape=request_shape,
                    )
            else:
                result = post_chat_completion(
                    body,
                    api_key=str(self.api_key),
                    url=self.chat_url,
                    timeout_seconds=self.timeout_seconds,
                    max_attempts=1,
                )
                headers = result.get("headers") if isinstance(result, dict) else {}
                if not result.get("ok"):
                    status = int(result.get("status_code") or 0) or None
                    err_payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
                    err = err_payload.get("error") if isinstance(err_payload, dict) else None
                    message = (
                        str(err.get("message"))
                        if isinstance(err, dict) and err.get("message")
                        else str(result.get("error") or f"HTTP {status}")
                    )
                    telem = extract_rate_limit_telemetry(
                        headers or result.get("headers"),
                        err_payload if isinstance(err_payload, dict) else None,
                    )
                    return ChatCompletionResult(
                        ok=False,
                        payload=err_payload if isinstance(err_payload, dict) else None,
                        status_code=status,
                        error=message[:400],
                        error_type=classify_provider_error(message, status_code=status),
                        provider=self.provider_name,
                        model=self.model,
                        retry_after=str(result.get("retry_after") or telem.get("retry_after") or "")
                        or None,
                        rate_limit_headers=dict(telem.get("rate_limit_headers") or {}),
                        finish_reason=telem.get("finish_reason"),
                        request_shape=request_shape,
                    )
                payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
                telem = extract_rate_limit_telemetry(headers or result.get("headers"), payload)
            content = parse_message_content(payload) if isinstance(payload, dict) else payload
            return ChatCompletionResult(
                ok=True,
                payload=payload if isinstance(payload, dict) else None,
                content=content,
                usage=extract_usage(payload) if isinstance(payload, dict) else {},
                provider=self.provider_name,
                model=self.model,
                finish_reason=telem.get("finish_reason"),
                retry_after=telem.get("retry_after"),
                rate_limit_headers=dict(telem.get("rate_limit_headers") or {}),
                request_shape=request_shape,
            )
        except GroqHttpError as exc:
            return ChatCompletionResult(
                ok=False,
                error=str(exc)[:400],
                status_code=getattr(exc, "status_code", None),
                error_type=classify_provider_error(str(exc), status_code=getattr(exc, "status_code", None)),
                provider=self.provider_name,
                model=self.model,
                request_shape=request_shape,
            )
        except Exception as exc:  # noqa: BLE001
            return ChatCompletionResult(
                ok=False,
                error=str(exc)[:400],
                error_type=classify_provider_error(str(exc)),
                provider=self.provider_name,
                model=self.model,
                request_shape=request_shape,
            )


@dataclass
class OpenAICompatibleTransport(ChatTransport):
    """Paid private Qwen / generic OpenAI-compatible chat. Idle unless configured."""

    api_key: str
    base_url: str
    model: str
    http_post: Callable[..., Any] | None = None
    timeout_seconds: int = 120
    provider_name: str = PROVIDER_QWEN

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        body_extra: dict[str, Any] | None = None,
        max_completion_tokens: int = V4_MAX_COMPLETION_TOKENS,
        temperature: float = 0.3,
        event_id: str | None = None,
        stage: str | None = None,
    ) -> ChatCompletionResult:
        # event_id/stage are accepted for writer parity with Kimi; unused on Qwen.
        _ = (event_id, stage)
        import requests

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_completion_tokens,
        }
        if body_extra:
            # Prefer max_tokens for OpenAI-compatible; drop Groq-only keys if present.
            extra = dict(body_extra)
            if "max_completion_tokens" in extra and "max_tokens" not in extra:
                extra["max_tokens"] = extra.pop("max_completion_tokens")
            body.update(extra)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            post = self.http_post or requests.post
            response = post(
                self.chat_url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
            status = getattr(response, "status_code", None)
            payload = response.json() if hasattr(response, "json") else response
            if status and int(status) >= 400:
                message = f"HTTP {status}"
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        message = str(err["message"])
                    elif isinstance(err, str):
                        message = err
                return ChatCompletionResult(
                    ok=False,
                    payload=payload if isinstance(payload, dict) else None,
                    status_code=int(status),
                    error=message[:400],
                    error_type=classify_provider_error(message, status_code=int(status)),
                    provider=self.provider_name,
                    model=self.model,
                )
            content = None
            if isinstance(payload, dict):
                choices = payload.get("choices")
                if isinstance(choices, list) and choices:
                    message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else {}
                    content = message.get("content") if isinstance(message, dict) else None
            return ChatCompletionResult(
                ok=True,
                payload=payload if isinstance(payload, dict) else None,
                content=content,
                provider=self.provider_name,
                model=self.model,
            )
        except Exception as exc:  # noqa: BLE001
            return ChatCompletionResult(
                ok=False,
                error=str(exc)[:400],
                error_type=classify_provider_error(str(exc)),
                provider=self.provider_name,
                model=self.model,
            )


@dataclass
class KimiChatTransport(ChatTransport):
    """Bedrock Mantle Kimi K2.5. Live only when NEWSAGENT_V2_V4_ALLOW_KIMI is set."""

    api_key: str = ""
    model: str = "moonshotai.kimi-k3"
    provider_name: str = PROVIDER_KIMI
    allowed: bool = False
    http_post: Callable[..., Any] | None = None
    timeout_seconds: int = 180
    base_url: str = ""
    event_id: str | None = None  # Budget tracking identity

    def __post_init__(self) -> None:
        if not self.base_url:
            from newsagent_v2.article.writer.bedrock_mantle import BASE_URL

            self.base_url = BASE_URL.rstrip("/")

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _get_stage_from_body_extra(self, body_extra: dict[str, Any] | None) -> str:
        """Extract stage from body_extra for audit."""
        if body_extra and isinstance(body_extra, dict):
            return str(body_extra.get("_stage", "unknown"))
        return "unknown"

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        body_extra: dict[str, Any] | None = None,
        max_completion_tokens: int = V4_MAX_COMPLETION_TOKENS,
        temperature: float = 0.3,
        event_id: str | None = None,
        stage: str | None = None,
    ) -> ChatCompletionResult:
        """Execute Kimi chat completion with budget guard.

        Args:
            messages: Chat messages
            body_extra: Extra body parameters (may include _stage for audit)
            max_completion_tokens: Maximum completion tokens
            temperature: Sampling temperature
            event_id: Story identifier for budget tracking
            stage: Operation stage (initial_writer, schema_fallback, etc.)
        """
        effective_event_id = event_id or self.event_id
        effective_stage = stage or self._get_stage_from_body_extra(body_extra)

        if not self.allowed:
            return ChatCompletionResult(
                ok=False,
                error="Kimi V4 transport idle: ALLOW_KIMI not enabled (no live call)",
                error_type=INFRA_OTHER,
                provider=self.provider_name,
                model=self.model,
            )
        if not str(self.api_key or "").strip():
            return ChatCompletionResult(
                ok=False,
                error="Kimi credentials missing",
                error_type=INFRA_AUTH,
                provider=self.provider_name,
                model=self.model,
            )

        # Budget guard check
        if effective_event_id:
            from newsagent_v2.providers.kimi_guard import KimiGuard, KimiBudgetError

            guard = KimiGuard(event_id=effective_event_id)
            check = guard.check_before_request(
                messages=messages,
                max_completion_tokens=max_completion_tokens,
                stage=effective_stage,
            )
            if not check.allowed:
                error_msg = f"KIMI_BUDGET_EXCEEDED: {check.reason or check.error}"
                return ChatCompletionResult(
                    ok=False,
                    error=error_msg,
                    error_type=INFRA_OTHER,
                    provider=self.provider_name,
                    model=self.model,
                )
        else:
            # Fail closed - no budget context
            return ChatCompletionResult(
                ok=False,
                error="KIMI_BUDGET_EXCEEDED: MISSING_EVENT_BUDGET_CONTEXT",
                error_type=INFRA_OTHER,
                provider=self.provider_name,
                model=self.model,
            )

        import requests

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_completion_tokens,
        }
        if body_extra:
            extra = dict(body_extra)
            if "max_completion_tokens" in extra and "max_tokens" not in extra:
                extra["max_tokens"] = extra.pop("max_completion_tokens")
            # Drop Groq-only reasoning knobs if present.
            extra.pop("reasoning_effort", None)
            # Drop internal stage marker if present
            extra.pop("_stage", None)
            body.update(extra)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        request_shape = {
            "provider": self.provider_name,
            "model": self.model,
            "max_tokens": max_completion_tokens,
            "has_response_format": "response_format" in body,
        }
        try:
            post = self.http_post or requests.post
            response = post(
                self.chat_url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
            status = getattr(response, "status_code", None)
            payload = response.json() if hasattr(response, "json") else response

            # Extract usage for reconciliation
            usage = extract_usage(payload) if isinstance(payload, dict) else {}

            # Reconcile budget (even on error responses)
            reconcile_info = None
            if effective_event_id:
                reconcile_info = guard.reconcile_after_request(
                    success=(status is not None and int(status) < 400),
                    usage=usage if usage else None,
                    http_status=int(status) if status else None,
                )

            if status and int(status) >= 400:
                message = f"HTTP {status}"
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        message = str(err["message"])
                    elif isinstance(err, str):
                        message = err
                return ChatCompletionResult(
                    ok=False,
                    payload=payload if isinstance(payload, dict) else None,
                    status_code=int(status),
                    error=sanitize_provider_log_blob(message)[:400],
                    error_type=classify_provider_error(message, status_code=int(status)),
                    provider=self.provider_name,
                    model=self.model,
                    request_shape=request_shape,
                )
            content = None
            finish_reason = None
            if isinstance(payload, dict):
                # Mantle/Kimi often returns fenced or brace-wrapped JSON; use the
                # soft parser. On failure raise so the writer can soft-retry
                # without json_schema.
                from newsagent_v2.article.writer.bedrock_mantle import parse_chat_payload

                content = parse_chat_payload(payload)
                choices = payload.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    finish_reason = choices[0].get("finish_reason")
            return ChatCompletionResult(
                ok=True,
                payload=payload if isinstance(payload, dict) else None,
                content=content,
                usage=usage,
                provider=self.provider_name,
                model=self.model,
                finish_reason=str(finish_reason) if finish_reason else None,
                request_shape=request_shape,
            )
        except Exception as exc:  # noqa: BLE001
            # Reconcile on exception
            if effective_event_id:
                guard.reconcile_after_request(
                    success=False,
                    usage=None,
                    http_status=None,
                    error=str(exc),
                )
            return ChatCompletionResult(
                ok=False,
                error=sanitize_provider_log_blob(str(exc))[:400],
                error_type=classify_provider_error(str(exc)),
                provider=self.provider_name,
                model=self.model,
                request_shape=request_shape,
            )


# Backward-compatible alias for idle/blocked semantics in older tests.
KimiBlockedTransport = KimiChatTransport


def build_transport(
    *,
    provider: str,
    model: str,
    environ: dict[str, str] | None,
    http_post: Callable[..., Any] | None = None,
    timeout_seconds: int = 120,
    allow_paid_qwen: bool = False,
    allow_kimi: bool = False,
) -> ChatTransport:
    name = (provider or DEFAULT_PROVIDER).strip().lower()
    if name == PROVIDER_GROQ:
        key = resolve_groq_api_key(environ) or ""
        return GroqChatTransport(
            api_key=key,
            model=model or DEFAULT_MODEL,
            http_post=http_post,
            timeout_seconds=timeout_seconds,
        )
    if name == PROVIDER_QWEN:
        from newsagent_v2.article.writer.qwen_vllm import BASE_ENV, KEY_ENV, load_qwen_config

        if not allow_paid_qwen:
            # Return a transport that refuses without calling network.
            return OpenAICompatibleTransport(
                api_key="blocked",
                base_url="http://127.0.0.1:9",
                model=model or "vllm-local/qwen3.8-27b",
                http_post=lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("paid private Qwen blocked: set NEWSAGENT_V2_V4_ALLOW_PAID_QWEN=1")
                ),
                timeout_seconds=timeout_seconds,
            )
        cfg = load_qwen_config(environ or {})
        return OpenAICompatibleTransport(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            model=model or cfg.model,
            http_post=http_post,
            timeout_seconds=timeout_seconds,
        )
    if name == PROVIDER_KIMI:
        from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV

        key = str((environ or {}).get(KIMI_KEY_ENV) or "").strip()
        return KimiChatTransport(
            api_key=key,
            model=model or "moonshotai.kimi-k3",
            base_url=str((environ or {}).get(ENV_KIMI_BASE_URL) or "").strip(),
            allowed=bool(allow_kimi),
            http_post=http_post,
            timeout_seconds=max(timeout_seconds, 180),
        )
    raise ValueError(f"unknown V4 writer provider {provider!r}")


def sanitize_provider_log_blob(blob: str) -> str:
    """Redact common secret patterns from diagnostic strings."""
    text = blob or ""
    text = re.sub(r"(Bearer\s+)[^\s\"']+", r"\1***", text, flags=re.I)
    text = re.sub(r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s]+", r"\1***", text, flags=re.I)
    return text


def transport_request_contains_no_secrets(body: dict[str, Any], secrets: tuple[str, ...]) -> bool:
    blob = json.dumps(body, ensure_ascii=False)
    for secret in secrets:
        if secret and secret in blob:
            return False
    return True
