"""AWS Nova Canvas image provider through the OpenAI-compatible proxy.

Used only as the fallback when the Vertex image path is blocked (daily Vertex
budget exhausted, or Vertex answered 429 / RESOURCE_EXHAUSTED / quota).

- Endpoint: ``{NEWSAGENT_V2_V4_KIMI_BASE_URL}/images/generations`` (same proxy and
  same key as the Kimi writer: ``NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY``).
- Model: ``NEWSAGENT_V2_NOVA_MODEL`` (default ``nova-canvas``), served by Bedrock
  ``amazon.nova-canvas-v1:0``.
- Text-to-image only. Reference images are never sent; the Python compositor still
  applies the site logo afterwards.
- One HTTP attempt per provider instance, no retries. Never logs or persists the key.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping

import requests
from PIL import Image

from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.provider import (
    ImageProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
)
from newsagent_v2.image.validate import file_sha256, refuse_overwrite

PROVIDER_NAME = "nova"
NOVA_BEDROCK_MODEL_ID = "amazon.nova-canvas-v1:0"
DEFAULT_PROXY_MODEL = "nova-canvas"
ACCEPTED_MODELS = frozenset({"nova-canvas", NOVA_BEDROCK_MODEL_ID})

ENV_FALLBACK_ENABLED = "NEWSAGENT_V2_NOVA_FALLBACK_ENABLED"
ENV_MODEL = "NEWSAGENT_V2_NOVA_MODEL"
# Reuse the proxy + key the Kimi writer already uses (see newsagent_v2.write.kimi).
ENV_BASE_URL = "NEWSAGENT_V2_V4_KIMI_BASE_URL"
ENV_API_KEY = "NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY"

PROMPT_CHAR_LIMIT = 1024
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_TIMEOUT_SECONDS = 120
HARD_MAX_GENERATION_CALLS = 1

# Bedrock Nova Canvas list price per image (USD). Flat per-image pricing.
NOVA_PRICE_USD = {
    "standard": {"up_to_1024": 0.04, "up_to_2048": 0.06},
    "premium": {"up_to_1024": 0.06, "up_to_2048": 0.08},
}
NOVA_PRICING_SOURCE = "AWS Bedrock Nova Canvas on-demand list price per image"


class NovaConfigError(ValueError):
    """Nova fallback is not configured. Message never contains secrets."""


@dataclass(frozen=True)
class NovaProxyConfig:
    base_url: str
    api_key: str
    model: str = DEFAULT_PROXY_MODEL
    quality: str = "standard"

    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,) if self.api_key else ()

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/generations"


def nova_fallback_enabled(environ: Mapping[str, str] | None) -> bool:
    """Default ON. Only an explicit false/0/no/off disables the fallback."""
    raw = str((environ or {}).get(ENV_FALLBACK_ENABLED, "") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def load_nova_proxy_config(environ: Mapping[str, str] | None) -> NovaProxyConfig:
    env = environ or {}
    base_url = str(env.get(ENV_BASE_URL) or "").strip().strip('"').strip("'")
    api_key = str(env.get(ENV_API_KEY) or "").strip().strip('"').strip("'")
    model = str(env.get(ENV_MODEL) or DEFAULT_PROXY_MODEL).strip() or DEFAULT_PROXY_MODEL
    missing = [name for name, value in ((ENV_BASE_URL, base_url), (ENV_API_KEY, api_key)) if not value]
    if missing:
        raise NovaConfigError("nova_config_incomplete: missing " + ", ".join(missing))
    if not base_url.lower().startswith("https://"):
        raise NovaConfigError(f"nova_config_invalid: {ENV_BASE_URL} must be an https URL")
    if model not in ACCEPTED_MODELS:
        raise NovaConfigError(
            f"nova_config_invalid: {ENV_MODEL} must be one of {sorted(ACCEPTED_MODELS)}"
        )
    return NovaProxyConfig(base_url=base_url, api_key=api_key, model=model)


def nova_price_usd(width: int | None, height: int | None, *, quality: str = "standard") -> float:
    """List price for one image. Either side above 1024 uses the 2048 tier (conservative)."""
    tiers = NOVA_PRICE_USD.get(quality, NOVA_PRICE_USD["standard"])
    w = int(width or 0)
    h = int(height or 0)
    if w and h and w <= 1024 and h <= 1024:
        return tiers["up_to_1024"]
    return tiers["up_to_2048"]


def fit_nova_prompt(prompt: str, limit: int = PROMPT_CHAR_LIMIT) -> str:
    """Collapse whitespace and keep the prompt within Nova's character limit.

    Cuts at the last sentence end that fits, then at a word boundary.
    """
    text = " ".join(str(prompt or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("; "))
    if cut >= int(limit * 0.5):
        return head[: cut + 1].strip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(" ,;:-")


def build_nova_prompt(story_brief: Mapping[str, Any] | None, fallback_prompt: str = "") -> str:
    """Short, positive-phrased hero-image prompt for Nova Canvas (<= 1024 chars).

    Nova follows positive descriptions best; the long Vertex prompt is mostly
    "do not render X" clauses and is over Nova's limit.
    """
    brief = story_brief or {}
    subject = " ".join(str(brief.get("primary_subject") or "").split())[:200]
    event = " ".join(str(brief.get("event") or "").split())[:200]
    if not subject:
        return fit_nova_prompt(fallback_prompt or "Premium financial-news editorial photograph.")
    parts = [
        "Premium financial-news editorial photograph, professional crypto and business newsroom hero image, wide 16:9 landscape.",
        f"Story subject: {subject}.",
    ]
    if event and event.lower() != subject.lower():
        parts.append(f"Event: {event}.")
    objects = [str(o) for o in (brief.get("important_objects") or []) if o][:5]
    if objects:
        parts.append("Include: " + "; ".join(objects) + ".")
    location = brief.get("location_if_supported")
    if location:
        parts.append(f"Setting: {location}.")
    parts.append(
        "One clear focal subject, clean institutional composition, strong depth, cinematic "
        "news-feature lighting, serious and restrained mood, realistic textures, generic "
        "unnamed people only. Clean open space in the top-right corner. Pure imagery, "
        "textless surfaces, blank screens."
    )
    return fit_nova_prompt(" ".join(parts))


class NovaHttpResponse:
    def __init__(self, status_code: int, content: bytes, headers: Any = None, text: str | None = None) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._text = text

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def default_transport(url: str, *, headers: dict[str, str], json_body: dict[str, Any], timeout: int) -> NovaHttpResponse:
    response = requests.post(url, headers=headers, json=json_body, timeout=timeout, allow_redirects=False)
    return NovaHttpResponse(
        status_code=response.status_code,
        content=response.content or b"",
        headers=response.headers,
        text=response.text,
    )


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name) or headers.get(name.lower())
    except Exception:
        return None
    return str(value) if value else None


def _error_message(payload: Any, text: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err.get("code") or "")
        if isinstance(err, str):
            return err
        if payload.get("message"):
            return str(payload.get("message"))
        if payload.get("detail"):
            return str(payload.get("detail"))
    return " ".join(str(text or "").split())[:300]


_SAFETY_RE = re.compile(r"content filter|filtered|safety|blocked|responsible ai|moderation|policy", re.I)


def classify_nova_http_error(status: int | None, message: str) -> str:
    blob = str(message or "").lower()
    if status in (401, 403):
        return "nova_auth_error"
    if status == 429 or "throttl" in blob or "too many requests" in blob:
        return "nova_rate_limited"
    if status == 404:
        return "nova_model_not_found"
    if status is not None and 400 <= status < 500 and _SAFETY_RE.search(blob):
        return "nova_safety_block"
    if status is not None and 400 <= status < 500:
        return "nova_bad_request"
    return "nova_http_error"


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    return ".png"


class NovaProxyImageProvider(ImageProvider):
    """Nova Canvas via proxy ``POST /v1/images/generations`` (OpenAI images API shape)."""

    reference_images_supported = False

    def __init__(
        self,
        config: NovaProxyConfig,
        *,
        transport: Callable[..., NovaHttpResponse] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self.model = config.model
        self._transport = transport or default_transport
        self.timeout_seconds = timeout_seconds
        self.generation_calls = 0

    def secrets(self) -> tuple[str, ...]:
        return self._config.secrets()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_name=PROVIDER_NAME,
            model_name=NOVA_BEDROCK_MODEL_ID,
            model_version=None,
            backend_type="remote_hosted",
            deployment_notes=(
                f"AWS Bedrock Nova Canvas via OpenAI-compatible proxy /images/generations "
                f"(proxy model id {self.model}). Text-to-image only; logo applied by Python compositor."
            ),
        )

    def _size(self, request: ProviderRequest) -> tuple[int, int]:
        width = int(request.width or DEFAULT_WIDTH)
        height = int(request.height or DEFAULT_HEIGHT)
        return width, height

    def wire_body(self, request: ProviderRequest) -> dict[str, Any]:
        width, height = self._size(request)
        return {
            "model": self.model,
            "prompt": fit_nova_prompt(request.prompt),
            "size": f"{width}x{height}",
            "n": 1,
            "response_format": "b64_json",
        }

    def recorded_request(self, request: ProviderRequest) -> dict[str, Any]:
        body = self.wire_body(request)
        payload = {
            "provider_name": PROVIDER_NAME,
            "model": NOVA_BEDROCK_MODEL_ID,
            "proxy_model": self.model,
            "endpoint_path": "/images/generations",
            "base_url_env": ENV_BASE_URL,
            "api_key_env": ENV_API_KEY,
            "method": "POST",
            "auth": "Bearer proxy key",
            "authorization_included": False,
            "allow_redirects": False,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": 0,
            "hard_max_generation_calls": HARD_MAX_GENERATION_CALLS,
            "body": body,
            "prompt_chars": len(body["prompt"]),
            "prompt_char_limit": PROMPT_CHAR_LIMIT,
            "negative_prompt_sent": False,
            "reference_images_supported": False,
            "reference_image_dropped": bool(request.reference_image_path),
        }
        return redact_secrets(payload, self.secrets())

    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        identity = self.identity()
        if self.generation_calls >= HARD_MAX_GENERATION_CALLS:
            return self._failure(identity, cold_start, 0, None, None, "hard_max_generation_calls_exceeded")
        self.generation_calls += 1

        body = self.wire_body(request)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        started = perf_counter()
        try:
            response = self._transport(
                self._config.endpoint, headers=headers, json_body=body, timeout=self.timeout_seconds
            )
        except requests.RequestException as exc:
            elapsed = int((perf_counter() - started) * 1000)
            return self._failure(identity, cold_start, elapsed, None, None, f"transport_error: {exc.__class__.__name__}")
        elapsed = int((perf_counter() - started) * 1000)
        status = response.status_code
        request_id = _header(response.headers, "x-request-id") or _header(response.headers, "x-amzn-requestid")
        content_type = _header(response.headers, "content-type")

        if status is not None and 300 <= status < 400:
            return self._failure(identity, cold_start, elapsed, status, request_id, "unsafe_redirect")

        try:
            payload = response.json()
        except Exception:
            payload = None

        if status != 200 or not isinstance(payload, dict) or isinstance(payload.get("error"), (dict, str)):
            message = _error_message(payload, response.text if payload is None else "")
            reason = classify_nova_http_error(status, message) if status != 200 else "nova_malformed_response"
            diagnostic = {
                "http_status": status,
                "response_content_type": content_type,
                "safe_error_message": str(redact_secrets(message[:320], self.secrets())),
            }
            return self._failure(identity, cold_start, elapsed, status, request_id, reason, diagnostic=diagnostic)

        data = payload.get("data")
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        b64 = first.get("b64_json")
        if not isinstance(b64, str) or not b64.strip():
            return self._failure(
                identity, cold_start, elapsed, status, request_id, "nova_missing_image",
                diagnostic={"http_status": status, "top_level_keys": sorted(str(k) for k in payload.keys())},
            )
        try:
            image_bytes = base64.b64decode(b64, validate=False)
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
                width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("zero_dimensions")
        except Exception:
            return self._failure(identity, cold_start, elapsed, status, request_id, "invalid_image_bytes")

        dest = Path(dest_path)
        suffix = _image_suffix(image_bytes)
        if dest.suffix.lower() != suffix:
            dest = dest.with_suffix(suffix)
        refuse_overwrite(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_bytes)

        req_w, req_h = self._size(request)
        cost = nova_price_usd(width, height, quality=self._config.quality)
        usage: dict[str, Any] = {
            "images": 1,
            "quality": self._config.quality,
            "returned_width": width,
            "returned_height": height,
            "list_price_usd": cost,
            "pricing_source": NOVA_PRICING_SOURCE,
            "prompt_chars": len(body["prompt"]),
        }
        if isinstance(payload.get("usage"), dict):
            usage["proxy_usage"] = payload["usage"]
        return ProviderResult(
            provider_name=identity.provider_name,
            success=True,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            total_latency_ms=elapsed,
            first_image_latency_ms=elapsed if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            width=width,
            height=height,
            retry_count=0,
            provider_reported_cost=cost,
            raw_image_path=str(dest),
            sha256=file_sha256(dest),
            http_status=status,
            request_latency_ms=elapsed,
            response_format=content_type or "application/json",
            size_bytes=len(image_bytes),
            provider_reported_usage=redact_secrets(usage, self.secrets()),
            requested_width=req_w,
            requested_height=req_h,
            provider_reported_cost_usd=cost,
            estimated_list_price_usd=cost,
            estimated_list_price_is_estimate=False,
            estimated_list_price_note=(
                f"{NOVA_PRICING_SOURCE}: ${cost:.2f} ({self._config.quality}); flat per image, "
                "so list price equals billed price."
            ),
            requested_aspect_ratio="16:9",
            provider_request_id=request_id,
            total_pipeline_latency_ms=elapsed,
            deployment_notes=identity.deployment_notes,
        )

    def _failure(
        self,
        identity: ProviderIdentity,
        cold_start: bool,
        elapsed: int,
        http_status: int | None,
        request_id: str | None,
        reason: str,
        *,
        diagnostic: dict[str, Any] | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider_name=identity.provider_name,
            success=False,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            total_latency_ms=elapsed,
            cold_start=cold_start,
            warm_generation=not cold_start,
            retry_count=0,
            failure_reason=reason,
            http_status=http_status,
            request_latency_ms=elapsed,
            provider_reported_usage=(
                {"_safe_response_diagnostic": redact_secrets(diagnostic, self.secrets())} if diagnostic else None
            ),
            provider_request_id=request_id,
            deployment_notes=identity.deployment_notes,
        )
