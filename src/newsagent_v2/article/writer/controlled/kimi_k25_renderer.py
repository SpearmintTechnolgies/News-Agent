"""Kimi K2.5 ProseRenderer. Idle by default. Live only under explicit fallback policy."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from newsagent_v2.article.writer.bedrock_mantle import (
    KIMI_MODEL,
    PROVIDER_NAME,
    REAL_INFERENCE_AUTHORIZED,
    BedrockMantleConfig,
    BedrockMantleConfigError,
    chat_completions_url,
    extract_usage,
    load_bedrock_mantle_config,
    no_retry_session,
    parse_chat_payload,
    sanitize_error,
)
from newsagent_v2.article.writer.controlled.editorial_dossier import build_editorial_dossier, dossier_messages
from newsagent_v2.article.writer.controlled.groq_oss20 import parse_controlled_v3_native
from newsagent_v2.article.writer.controlled.plan import ArticlePlan
from newsagent_v2.article.writer.controlled.renderer import RendererResult
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers

FALLBACK_ENV = "NEWSAGENT_V2_KIMI_WRITER_FALLBACK"
DEFAULT_MAX_COMPLETION_TOKENS = 2500
DEFAULT_TIMEOUT_SECONDS = 180


def kimi_fallback_permitted(environ: dict[str, str] | None) -> bool:
    raw = str((environ or {}).get(FALLBACK_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes"}


def should_fallback_to_kimi(*, failure_class: str | None, environ: dict[str, str] | None) -> bool:
    """QA failures never select Kimi. Only explicit fallback + provider unavailability."""
    if not kimi_fallback_permitted(environ):
        return False
    return failure_class == "WRITER_PROVIDER_ERROR"


def _auth_headers(config: BedrockMantleConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }


def _build_real_transport(config: BedrockMantleConfig) -> Any:
    session = no_retry_session()

    def transport(
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        timeout: int,
    ) -> Any:
        del headers
        return session.request(
            method,
            url,
            headers=_auth_headers(config),
            json=json_body,
            timeout=timeout,
            allow_redirects=False,
        )

    return transport


class KimiK25ProseRenderer:
    renderer_name = "kimi_k25_prose"
    provider_name = PROVIDER_NAME
    model_name = KIMI_MODEL

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        transport: Any = None,
        allow_real_http: bool = False,
        max_calls: int = 1,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.environ = environ or {}
        self.transport = transport
        # Explicit delivery fallback may authorize real HTTP without flipping the global latch.
        self.allow_real_http = bool(
            allow_real_http and (REAL_INFERENCE_AUTHORIZED or kimi_fallback_permitted(self.environ))
        )
        # Per-candidate: one article + optional supplemental.
        self.max_calls = max(1, int(max_calls))
        self.timeout_seconds = timeout_seconds
        self.generation_calls = 0
        self.http_status: int | None = None
        self.latency_ms: int | None = None
        self.usage: dict[str, Any] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        self.error: str | None = None
        self.native: dict[str, Any] | None = None
        self.telemetry: dict[str, Any] = {
            "provider": PROVIDER_NAME,
            "model": KIMI_MODEL,
            "generation_calls": 0,
            "real_http_attempted": False,
            "fallback_policy": kimi_fallback_permitted(self.environ),
        }

    def render(self, plan: ArticlePlan, ledgers: EvidenceLedgers) -> RendererResult:
        if self.generation_calls >= self.max_calls:
            self.error = "kimi generation cap"
            return RendererResult(ok=False, provider_error=True, error=self.error)
        if not self.allow_real_http and self.transport is None:
            self.error = "Kimi fallback idle: no live call (policy/default block)"
            self.telemetry = {
                "provider": PROVIDER_NAME,
                "model": KIMI_MODEL,
                "generation_calls": 0,
                "real_http_attempted": False,
                "failure_class": "WRITER_PROVIDER_ERROR",
                "fallback_policy": kimi_fallback_permitted(self.environ),
            }
            return RendererResult(ok=False, provider_error=True, error=self.error)
        try:
            cfg = load_bedrock_mantle_config(self.environ)
        except BedrockMantleConfigError as exc:
            self.error = str(exc)
            self.telemetry = {
                "provider": PROVIDER_NAME,
                "model": KIMI_MODEL,
                "generation_calls": self.generation_calls,
                "real_http_attempted": False,
                "failure_class": "WRITER_PROVIDER_ERROR",
                "fallback_policy": kimi_fallback_permitted(self.environ),
            }
            return RendererResult(ok=False, provider_error=True, error=self.error)

        dossier = build_editorial_dossier(plan, ledgers)
        body = {
            "model": cfg.model,
            "messages": dossier_messages(dossier),
            "max_tokens": DEFAULT_MAX_COMPLETION_TOKENS,
            "temperature": 0.4,
        }
        poster = self.transport if self.transport is not None else _build_real_transport(cfg)
        self.generation_calls += 1
        started = perf_counter()
        try:
            response = poster(
                chat_completions_url(),
                method="POST",
                headers=_auth_headers(cfg),
                json_body=body,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            self.latency_ms = int((perf_counter() - started) * 1000)
            self.error = sanitize_error(type(exc).__name__, cfg.secrets())
            self.telemetry = _tel(self, failure="WRITER_PROVIDER_ERROR", real=True)
            return RendererResult(ok=False, provider_error=True, error=self.error)

        self.latency_ms = int((perf_counter() - started) * 1000)
        status = getattr(response, "status_code", None)
        self.http_status = int(status) if isinstance(status, int) else None
        try:
            payload = response.json() if hasattr(response, "json") else None
        except Exception:
            payload = None
        if not isinstance(payload, dict) or self.http_status != 200:
            self.error = sanitize_error(f"HTTP {self.http_status}", cfg.secrets())
            self.telemetry = _tel(self, failure="WRITER_PROVIDER_ERROR", real=True)
            return RendererResult(ok=False, provider_error=True, error=self.error)
        self.usage = extract_usage(payload)
        try:
            native = parse_chat_payload(payload)
        except Exception as exc:
            self.error = sanitize_error(str(exc), cfg.secrets())
            self.telemetry = _tel(self, failure="WRITER_OUTPUT_INVALID", real=True)
            return RendererResult(ok=False, invalid_output=True, error=self.error)
        parsed = parse_controlled_v3_native(native, plan)
        self.native = parsed.native
        self.error = parsed.error
        self.telemetry = _tel(self, failure=None if parsed.ok else "WRITER_OUTPUT_INVALID", real=True)
        return parsed


def _tel(renderer: KimiK25ProseRenderer, *, failure: str | None, real: bool) -> dict[str, Any]:
    return {
        "provider": PROVIDER_NAME,
        "model": KIMI_MODEL,
        "latency_ms": renderer.latency_ms,
        "http_status": renderer.http_status,
        "prompt_tokens": renderer.usage.get("prompt_tokens"),
        "completion_tokens": renderer.usage.get("completion_tokens"),
        "generation_calls": renderer.generation_calls,
        "failure_class": failure,
        "real_http_attempted": real,
        "fallback_policy": kimi_fallback_permitted(renderer.environ),
    }
