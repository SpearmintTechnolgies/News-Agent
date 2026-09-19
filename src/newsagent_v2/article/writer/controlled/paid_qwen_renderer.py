"""Paid Qwen ProseRenderer. OpenAI-compatible endpoint. Not Groq."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from newsagent_v2.article.writer.controlled.editorial_dossier import build_editorial_dossier, dossier_messages
from newsagent_v2.article.writer.controlled.groq_oss20 import parse_controlled_v3_native
from newsagent_v2.article.writer.controlled.plan import ArticlePlan
from newsagent_v2.article.writer.controlled.renderer import RendererResult
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.qwen_vllm import (
    QWEN_MODEL,
    QwenVLLMConfig,
    QwenVLLMConfigError,
    api_join,
    credential_presence,
    extract_usage,
    load_qwen_config,
    parse_chat_payload,
    sanitize_error,
)

PROVIDER_NAME = "paid_qwen"
DEFAULT_MAX_COMPLETION_TOKENS = 2500
DEFAULT_TIMEOUT_SECONDS = 180


class PaidQwenProseRenderer:
    renderer_name = "paid_qwen_prose"
    provider_name = PROVIDER_NAME
    model_name = QWEN_MODEL

    def __init__(
        self,
        *,
        config: QwenVLLMConfig | None = None,
        environ: dict[str, str] | None = None,
        http_post: Any = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_calls: int = 1,
    ) -> None:
        self.config = config
        self.environ = environ
        self.http_post = http_post
        self.timeout_seconds = timeout_seconds
        self.max_calls = max(1, int(max_calls))
        self.generation_calls = 0
        self.http_status: int | None = None
        self.latency_ms: int | None = None
        self.usage: dict[str, Any] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        self.error: str | None = None
        self.native: dict[str, Any] | None = None
        self.telemetry: dict[str, Any] = {}

    def _resolve_config(self) -> QwenVLLMConfig:
        if self.config is not None:
            return self.config
        return load_qwen_config(self.environ)

    def render(self, plan: ArticlePlan, ledgers: EvidenceLedgers) -> RendererResult:
        if self.generation_calls >= self.max_calls:
            return RendererResult(ok=False, provider_error=True, error="generation budget exhausted")
        try:
            cfg = self._resolve_config()
        except QwenVLLMConfigError as exc:
            self.error = str(exc)
            self.telemetry = {
                "provider": PROVIDER_NAME,
                "model": QWEN_MODEL,
                "failure_class": "WRITER_PROVIDER_ERROR",
                "generation_calls": self.generation_calls,
            }
            return RendererResult(ok=False, provider_error=True, error=self.error)
        dossier = build_editorial_dossier(plan, ledgers)
        body = {
            "model": cfg.model,
            "messages": dossier_messages(dossier),
            "max_tokens": DEFAULT_MAX_COMPLETION_TOKENS,
            "temperature": 0.4,
        }
        poster = self.http_post
        if poster is None:
            self.error = "paid Qwen HTTP disabled without injected transport"
            self.telemetry = {
                "provider": PROVIDER_NAME,
                "model": cfg.model,
                "failure_class": "WRITER_PROVIDER_ERROR",
                "generation_calls": 0,
                "http_attempted": False,
            }
            return RendererResult(ok=False, provider_error=True, error=self.error)
        self.generation_calls += 1
        started = perf_counter()
        url = api_join(cfg.base_url, "chat/completions")
        try:
            response = poster(
                url,
                method="POST",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json_body=body,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            self.latency_ms = int((perf_counter() - started) * 1000)
            self.error = sanitize_error(type(exc).__name__, cfg.secrets())
            self.telemetry = _tel(cfg, self, failure="WRITER_PROVIDER_ERROR")
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
            self.telemetry = _tel(cfg, self, failure="WRITER_PROVIDER_ERROR")
            return RendererResult(ok=False, provider_error=True, error=self.error)
        self.usage = extract_usage(payload)
        try:
            native = parse_chat_payload(payload)
        except Exception as exc:
            self.error = sanitize_error(str(exc), cfg.secrets())
            self.telemetry = _tel(cfg, self, failure="WRITER_OUTPUT_INVALID")
            return RendererResult(ok=False, invalid_output=True, error=self.error)
        parsed = parse_controlled_v3_native(native, plan)
        self.native = parsed.native
        self.error = parsed.error
        self.telemetry = _tel(cfg, self, failure=None if parsed.ok else "WRITER_OUTPUT_INVALID")
        return parsed


def _tel(cfg: QwenVLLMConfig, renderer: PaidQwenProseRenderer, *, failure: str | None) -> dict[str, Any]:
    return {
        "provider": PROVIDER_NAME,
        "model": cfg.model,
        "latency_ms": renderer.latency_ms,
        "http_status": renderer.http_status,
        "prompt_tokens": renderer.usage.get("prompt_tokens"),
        "completion_tokens": renderer.usage.get("completion_tokens"),
        "generation_calls": renderer.generation_calls,
        "failure_class": failure,
    }


def paid_qwen_configured(environ: dict[str, str] | None) -> dict[str, bool]:
    return credential_presence(environ or {})
