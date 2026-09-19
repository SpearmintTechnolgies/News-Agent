"""
Google Gemini image provider (Nano Banana 2 Lite / gemini-3.1-flash-lite-image).

Reads NEWSAGENT_V2_GEMINI_API_KEY from an explicit environ mapping.
Never logs or persists the key. No automatic retries.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

import requests
from PIL import Image

from newsagent_v2.benchmark.telemetry import ESTIMATE_USD_TO_INR, redact_secrets
from newsagent_v2.image.provider import (
    ImageProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
)
from newsagent_v2.image.reference import with_reference_prompt
from newsagent_v2.image.validate import file_sha256, refuse_overwrite, validate_reference_image

KEY_ENV = "NEWSAGENT_V2_GEMINI_API_KEY"
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
DEFAULT_TIMEOUT_SECONDS = 60
REQUESTED_ASPECT_RATIO = "16:9"
REQUESTED_RESOLUTION_TIER = "1K"

# Diagnostic list prices from https://ai.google.dev/gemini-api/docs/pricing
# May be stale. NOT billed cost.
LIST_INPUT_USD_PER_MILLION = 0.25
LIST_TEXT_OUTPUT_USD_PER_MILLION = 1.50
LIST_IMAGE_OUTPUT_USD_PER_MILLION = 30.00
LIST_PRICE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"


class GeminiConfigError(ValueError):
    """Missing or invalid V2 Gemini configuration."""


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str

    def __post_init__(self) -> None:
        key = self.api_key.strip()
        if not key:
            raise GeminiConfigError(f"{KEY_ENV} is empty")
        object.__setattr__(self, "api_key", key)

    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,)


def load_gemini_config(environ: dict[str, str] | None) -> GeminiConfig:
    if environ is None:
        raise GeminiConfigError(
            "environ must be provided explicitly; process-wide fallback is not used"
        )
    raw = environ.get(KEY_ENV)
    if raw is None:
        raise GeminiConfigError(f"{KEY_ENV} is not set")
    return GeminiConfig(api_key=str(raw))


def generate_content_url(model: str) -> str:
    return f"{API_BASE}/models/{model}:generateContent"


def _header(headers: Any, name: str) -> str | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None and hasattr(headers, "items"):
        for key, item in headers.items():
            if str(key).lower() == name.lower():
                return str(item)
    return None if value is None else str(value)


def _extract_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("status")
            if message:
                return str(message)
        if payload.get("message"):
            return str(payload["message"])
    return fallback


def _first_inline_image(payload: dict[str, Any]) -> tuple[bytes, str] | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
            data = inline.get("data")
            if not isinstance(data, str) or not data.strip():
                continue
            if mime and not mime.lower().startswith("image/"):
                continue
            try:
                raw = base64.b64decode(data, validate=False)
            except Exception:
                continue
            if not raw:
                continue
            return raw, mime or "image/png"
    return None


def _image_suffix(data: bytes, mime: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"
    if "webp" in mime:
        return ".webp"
    return ".png"


def _usage_blobs(payload: dict[str, Any]) -> tuple[Any, Any, Any]:
    usage = payload.get("usageMetadata") or payload.get("usage_metadata")
    if not isinstance(usage, dict):
        usage = None
    tokens = None
    image_usage = None
    if isinstance(usage, dict):
        tokens = {
            "promptTokenCount": usage.get("promptTokenCount", usage.get("prompt_token_count")),
            "candidatesTokenCount": usage.get(
                "candidatesTokenCount", usage.get("candidates_token_count")
            ),
            "totalTokenCount": usage.get("totalTokenCount", usage.get("total_token_count")),
        }
        details = usage.get("candidatesTokensDetails") or usage.get(
            "candidates_tokens_details"
        )
        if isinstance(details, list):
            image_usage = details
    return usage, image_usage, tokens


def _image_and_text_output_tokens(image_usage: Any, tokens: Any) -> tuple[int | None, int | None]:
    image_tokens = None
    if isinstance(image_usage, list):
        total = 0
        found = False
        for row in image_usage:
            if not isinstance(row, dict):
                continue
            modality = str(row.get("modality") or "").upper()
            count = row.get("tokenCount", row.get("token_count"))
            if modality == "IMAGE" and isinstance(count, int):
                total += count
                found = True
        if found:
            image_tokens = total
    text_tokens = None
    if isinstance(tokens, dict):
        candidates = tokens.get("candidatesTokenCount")
        if isinstance(candidates, int):
            if image_tokens is not None:
                text_tokens = max(0, candidates - image_tokens)
            else:
                text_tokens = candidates
    return image_tokens, text_tokens


def estimate_list_price_usd(
    *,
    prompt_tokens: int | None,
    image_output_tokens: int | None,
    text_output_tokens: int | None,
) -> float | None:
    if prompt_tokens is None and image_output_tokens is None and text_output_tokens is None:
        return None
    usd = 0.0
    if prompt_tokens is not None:
        usd += (prompt_tokens / 1_000_000) * LIST_INPUT_USD_PER_MILLION
    if image_output_tokens is not None:
        usd += (image_output_tokens / 1_000_000) * LIST_IMAGE_OUTPUT_USD_PER_MILLION
    if text_output_tokens is not None:
        usd += (text_output_tokens / 1_000_000) * LIST_TEXT_OUTPUT_USD_PER_MILLION
    return round(usd, 8)


class GeminiHttpResponse:
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


def default_transport(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: int,
) -> GeminiHttpResponse:
    response = requests.post(
        url,
        headers=headers,
        json=json_body,
        timeout=timeout,
        allow_redirects=False,
    )
    return GeminiHttpResponse(
        status_code=response.status_code,
        content=response.content or b"",
        headers=response.headers,
        text=response.text,
    )


class GeminiImageProvider(ImageProvider):
    reference_images_supported = True
    def __init__(
        self,
        config: GeminiConfig,
        *,
        model: str = DEFAULT_MODEL,
        transport: Callable[..., GeminiHttpResponse] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise GeminiConfigError("model must be a non-empty string")
        self._config = config
        self.model = model.strip()
        self._transport = transport or default_transport
        self.timeout_seconds = timeout_seconds

    def secrets(self) -> tuple[str, ...]:
        return self._config.secrets()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_name="google_gemini",
            model_name=self.model,
            model_version=None,
            backend_type="remote_hosted",
            license_name=None,
            commercial_use_status=None,
            license_source=None,
            deployment_notes="Google Gemini generateContent image API. License/commercial status unverified here.",
        )

    def _reference_inline_part(self, request: ProviderRequest) -> dict[str, Any] | None:
        if not request.reference_image_path:
            return None
        inspected = validate_reference_image(Path(request.reference_image_path))
        raw = Path(request.reference_image_path).read_bytes()
        mime = str(inspected.get("mime_type") or "image/png")
        return {
            "inlineData": {
                "mimeType": mime,
                "data": base64.b64encode(raw).decode("ascii"),
            }
        }

    def wire_body(self, request: ProviderRequest) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        reference_part = self._reference_inline_part(request)
        prompt = request.prompt
        if reference_part is not None:
            parts.append(reference_part)
            prompt = with_reference_prompt(request.prompt, role=request.reference_image_role)
        parts.append({"text": prompt})
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": REQUESTED_ASPECT_RATIO,
                    "imageSize": REQUESTED_RESOLUTION_TIER,
                },
            },
        }

    def recorded_request(self, request: ProviderRequest) -> dict[str, Any]:
        body = self.wire_body(request)
        recorded_body = json.loads(json.dumps(body))
        parts = recorded_body.get("contents", [{}])[0].get("parts", [])
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                data = str(inline["data"])
                inline["data"] = f"[omitted {len(data)} base64 chars]"
                if request.reference_image_path:
                    inline["sha256"] = file_sha256(Path(request.reference_image_path))
        payload = {
            "provider_name": "google_gemini",
            "model": self.model,
            "endpoint": f"{API_BASE}/models/{{model}}:generateContent",
            "method": "POST",
            "auth": "x-goog-api-key header",
            "authorization_included": False,
            "api_key_in_url": False,
            "allow_redirects": False,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": 0,
            "body": recorded_body,
            "requested_aspect_ratio": REQUESTED_ASPECT_RATIO,
            "requested_resolution_tier": REQUESTED_RESOLUTION_TIER,
            "translation_applied": True,
            "translation_reason": (
                "Gemini image generateContent uses aspectRatio/imageSize rather than "
                "pixel width/height. Prompt text is unchanged unless a reference image "
                "is attached, in which case a fixed reference-use instruction block is appended."
            ),
            "reference_images_supported": True,
            "reference_image_attached": bool(request.reference_image_path),
            "reference_image_role": request.reference_image_role,
            "reference_required": request.reference_required,
        }
        return redact_secrets(payload, self.secrets())

    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        identity = self.identity()
        started = perf_counter()
        url = generate_content_url(self.model)
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._config.api_key,
        }
        body = self.wire_body(request)
        try:
            response = self._transport(
                url,
                headers=headers,
                json_body=body,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            elapsed = int((perf_counter() - started) * 1000)
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=None,
                request_id=None,
                reason=f"transport_error: {exc.__class__.__name__}",
            )

        elapsed = int((perf_counter() - started) * 1000)
        status = response.status_code
        request_id = _header(response.headers, "x-request-id") or _header(
            response.headers, "x-goog-request-id"
        )

        if status is not None and 300 <= status < 400:
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason="unsafe_redirect",
            )

        try:
            payload = response.json()
        except Exception:
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason="malformed_json",
            )

        if not isinstance(payload, dict):
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason="malformed_response",
            )

        request_id = payload.get("responseId") or payload.get("response_id") or request_id
        usage, image_usage, tokens = _usage_blobs(payload)

        if status != 200:
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason=_extract_error_message(payload, f"http_{status}"),
                usage=usage,
                image_usage=image_usage,
                tokens=tokens,
            )

        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                finish = str(first.get("finishReason") or first.get("finish_reason") or "")
                if finish.upper() in {"SAFETY", "BLOCKED", "PROHIBITED_CONTENT"}:
                    return self._failure(
                        identity=identity,
                        cold_start=cold_start,
                        elapsed=elapsed,
                        http_status=status,
                        request_id=request_id,
                        reason=f"blocked:{finish}",
                        usage=usage,
                        image_usage=image_usage,
                        tokens=tokens,
                    )

        extracted = _first_inline_image(payload)
        if extracted is None:
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason="missing_image",
                usage=usage,
                image_usage=image_usage,
                tokens=tokens,
            )

        image_bytes, mime = extracted
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image.load()
                returned_w, returned_h = image.size
                if returned_w <= 0 or returned_h <= 0:
                    raise ValueError("zero_dimensions")
        except Exception:
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason="invalid_image_bytes",
                usage=usage,
                image_usage=image_usage,
                tokens=tokens,
            )

        dest = Path(dest_path)
        suffix = _image_suffix(image_bytes, mime)
        if dest.suffix.lower() != suffix:
            dest = dest.with_suffix(suffix)
        refuse_overwrite(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_bytes)

        prompt_tokens = tokens.get("promptTokenCount") if isinstance(tokens, dict) else None
        image_tokens, text_tokens = _image_and_text_output_tokens(image_usage, tokens)
        estimate_usd = estimate_list_price_usd(
            prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
            image_output_tokens=image_tokens,
            text_output_tokens=text_tokens,
        )
        estimate_inr = (
            round(estimate_usd * ESTIMATE_USD_TO_INR, 6) if estimate_usd is not None else None
        )

        billed = None
        if isinstance(payload.get("cost"), (int, float)):
            billed = float(payload["cost"])

        return ProviderResult(
            provider_name=identity.provider_name,
            success=True,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            load_time_ms=None,
            generation_time_ms=None,
            total_latency_ms=elapsed,
            first_image_latency_ms=elapsed if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            width=returned_w,
            height=returned_h,
            retry_count=0,
            failure_reason=None,
            provider_reported_cost=billed,
            actual_cost_inr=None,
            raw_image_path=str(dest),
            sha256=file_sha256(dest),
            http_status=status,
            request_latency_ms=elapsed,
            response_format=mime,
            size_bytes=len(image_bytes),
            provider_reported_usage=usage,
            provider_reported_neurons=None,
            requested_width=None,
            requested_height=None,
            provider_reported_cost_usd=billed,
            provider_reported_image_usage=image_usage,
            provider_reported_tokens=tokens,
            provider_reported_latency=None,
            estimated_list_price_usd=estimate_usd,
            estimated_list_price_inr=estimate_inr,
            estimated_list_price_is_estimate=True if estimate_usd is not None else None,
            estimated_list_price_note=(
                "Diagnostic list-price estimate from published Gemini token rates. "
                f"Not billed cost. Source: {LIST_PRICE_SOURCE}"
                if estimate_usd is not None
                else None
            ),
            requested_aspect_ratio=REQUESTED_ASPECT_RATIO,
            requested_resolution_tier=REQUESTED_RESOLUTION_TIER,
            provider_request_id=str(request_id) if request_id else None,
            total_pipeline_latency_ms=elapsed,
            deployment_notes=identity.deployment_notes,
        )

    def _failure(
        self,
        *,
        identity: ProviderIdentity,
        cold_start: bool,
        elapsed: int,
        http_status: int | None,
        request_id: str | None,
        reason: str,
        usage: Any = None,
        image_usage: Any = None,
        tokens: Any = None,
    ) -> ProviderResult:
        sanitized = str(redact_secrets(reason, self.secrets()))
        return ProviderResult(
            provider_name=identity.provider_name,
            success=False,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            total_latency_ms=elapsed,
            first_image_latency_ms=elapsed if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            retry_count=0,
            failure_reason=sanitized,
            provider_reported_cost=None,
            actual_cost_inr=None,
            http_status=http_status,
            request_latency_ms=elapsed,
            provider_reported_usage=usage,
            provider_reported_cost_usd=None,
            provider_reported_image_usage=image_usage,
            provider_reported_tokens=tokens,
            provider_reported_latency=None,
            estimated_list_price_usd=None,
            estimated_list_price_inr=None,
            requested_aspect_ratio=REQUESTED_ASPECT_RATIO,
            requested_resolution_tier=REQUESTED_RESOLUTION_TIER,
            provider_request_id=str(request_id) if request_id else None,
            total_pipeline_latency_ms=elapsed,
        )
