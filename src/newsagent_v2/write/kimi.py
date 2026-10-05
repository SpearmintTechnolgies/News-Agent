"""Kimi K3 chat client (Bedrock Mantle, OpenAI-compatible) with a per-article budget."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import requests

logger = logging.getLogger(__name__)

KEY_ENV = "NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY"
BASE_URL = "https://bedrock-mantle.us-east-1.api.aws/v1"
KIMI_MODEL = "moonshotai.kimi-k3"

ENV_BASE_URL = "NEWSAGENT_V2_V4_KIMI_BASE_URL"
ENV_MODEL = "NEWSAGENT_V2_V4_WRITER_MODEL"
REQUEST_TIMEOUT_SECONDS = 300
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class KimiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


class BudgetExceeded(KimiError):
    pass


@dataclass
class StoryBudget:
    max_calls: int = 3
    max_tokens: int = 60_000
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def check(self, stage: str) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"article budget: {self.calls}/{self.max_calls} Kimi calls used before {stage}")
        if self.total_tokens >= self.max_tokens:
            raise BudgetExceeded(f"article budget: {self.total_tokens}/{self.max_tokens} tokens used before {stage}")

    def record(self, stage: str, usage: Mapping[str, Any], seconds: float, finish: str | None) -> None:
        self.calls += 1
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.log.append(
            {"stage": stage, "prompt_tokens": prompt, "completion_tokens": completion,
             "seconds": round(seconds, 1), "finish_reason": finish}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "max_tokens": self.max_tokens,
            "log": list(self.log),
        }


def parse_json_content(raw: str) -> dict[str, Any]:
    text = _FENCE_RE.sub("", _THINK_RE.sub("", raw or "").strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model reply contained no JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model reply JSON was not an object")
    return parsed


HttpPost = Callable[..., Any]


class KimiClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        model: str = KIMI_MODEL,
        http_post: HttpPost | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise KimiError(f"Kimi API key missing ({KEY_ENV})")
        self.api_key = api_key
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.model = model or KIMI_MODEL
        self.http_post = http_post or requests.post
        self.timeout = timeout

    @classmethod
    def from_env(cls, environ: Mapping[str, str], **kwargs: Any) -> "KimiClient":
        return cls(
            str(environ.get(KEY_ENV) or "").strip(),
            base_url=str(environ.get(ENV_BASE_URL) or BASE_URL).strip(),
            model=str(environ.get(ENV_MODEL) or KIMI_MODEL).strip(),
            **kwargs,
        )

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        budget: StoryBudget,
        stage: str,
        max_tokens: int = 16_000,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        """One budgeted call that must return a JSON object. One retry on a transient HTTP error."""
        attempt = 0
        while True:
            budget.check(stage)
            started = time.monotonic()
            try:
                payload, status = self._post(messages, max_tokens, temperature)
            except KimiError as exc:
                budget.record(stage, {}, time.monotonic() - started, "error")
                if exc.transient and attempt == 0:
                    attempt += 1
                    time.sleep(8)
                    continue
                raise
            choice = (payload.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            budget.record(stage, payload.get("usage") or {}, time.monotonic() - started, finish)
            content = (choice.get("message") or {}).get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
            if finish == "length":
                raise KimiError(f"{stage}: reply cut off at max_tokens={max_tokens}")
            try:
                return parse_json_content(str(content or ""))
            except (ValueError, json.JSONDecodeError) as exc:
                raise KimiError(f"{stage}: reply was not valid JSON ({exc})") from exc

    def _post(self, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> tuple[dict[str, Any], int]:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            resp = self.http_post(f"{self.base_url}/chat/completions", headers=headers, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise KimiError(f"Kimi request failed: {type(exc).__name__}", transient=True) from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if status >= 400:
            err = payload.get("error") if isinstance(payload, dict) else None
            message = err.get("message") if isinstance(err, dict) else (err or f"HTTP {status}")
            raise KimiError(f"Kimi HTTP {status}: {str(message)[:300]}", status=status,
                            transient=status in TRANSIENT_STATUSES)
        if not isinstance(payload, dict):
            raise KimiError("Kimi returned a non-object payload")
        return payload, status
