"""
Cloudflare Workers AI image provider.

Reads NEWSAGENT_V2_CLOUDFLARE_ACCOUNT_ID and NEWSAGENT_V2_CLOUDFLARE_API_TOKEN
only from an explicit environ mapping. Never logs or persists the token.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from urllib.parse import quote

import requests
from PIL import Image

from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.provider import (
    ImageProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
)
from newsagent_v2.image.reference import with_reference_prompt
from newsagent_v2.image.validate import (
    ImageValidationError,
    file_sha256,
    refuse_overwrite,
    validate_reference_image,
)

ACCOUNT_ENV = "NEWSAGENT_V2_CLOUDFLARE_ACCOUNT_ID"
TOKEN_ENV = "NEWSAGENT_V2_CLOUDFLARE_API_TOKEN"
API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_RETRIES = 1
MAX_RETRY_AFTER_SECONDS = 30
DEFAULT_RETRY_AFTER_SECONDS = 1
MIN_DIM = 256
MAX_DIM = 1920
# Closest practical 16:9 within Workers AI 256–1920 bounds.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
REFERENCE_FIELD = "input_image_0"
# Official Klein docs: all input images must be smaller than 512x512.
MAX_REFERENCE_EDGE = 511
REFERENCE_MIME = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class CloudflareConfigError(ValueError):
    """Missing or invalid Cloudflare Workers AI configuration."""


@dataclass(frozen=True)
class CloudflareConfig:
    account_id: str
    api_token: str

    def __post_init__(self) -> None:
        account = self.account_id.strip()
        token = self.api_token.strip()
        if not account:
            raise CloudflareConfigError(f"{ACCOUNT_ENV} is empty")
        if not token:
            raise CloudflareConfigError(f"{TOKEN_ENV} is empty")
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "api_token", token)

    def secrets(self) -> tuple[str, ...]:
        return (self.api_token, self.account_id)


def load_cloudflare_config(environ: dict[str, str] | None) -> CloudflareConfig:
    if environ is None:
        raise CloudflareConfigError(
            "environ must be provided explicitly; process-wide fallback is not used"
        )
    account = environ.get(ACCOUNT_ENV)
    token = environ.get(TOKEN_ENV)
    if account is None:
        raise CloudflareConfigError(f"{ACCOUNT_ENV} is not set")
    if token is None:
        raise CloudflareConfigError(f"{TOKEN_ENV} is not set")
    return CloudflareConfig(account_id=str(account), api_token=str(token))


def mask_token(token: str | None) -> str:
    if not token:
        return "[REDACTED]"
    cleaned = str(token).strip()
    if len(cleaned) < 10:
        return "[REDACTED]"
    return f"{cleaned[:3]}…{cleaned[-2:]}"


def sanitize_cloudflare_value(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    return redact_secrets(value, secrets)


def workers_ai_url(account_id: str, model: str) -> str:
    encoded_model = quote(model, safe="/@._-")
    return f"{API_BASE}/accounts/{account_id}/ai/run/{encoded_model}"


def clamp_dimension(value: int | None, fallback: int) -> int:
    if value is None:
        return fallback
    if value < MIN_DIM or value > MAX_DIM:
        raise CloudflareConfigError(
            f"dimension {value} is outside Cloudflare image range {MIN_DIM}-{MAX_DIM}"
        )
    return int(value)


def parse_retry_after(header: str | None) -> float:
    if not header:
        return float(DEFAULT_RETRY_AFTER_SECONDS)
    text = str(header).strip()
    try:
        seconds = float(text)
    except ValueError:
        return float(DEFAULT_RETRY_AFTER_SECONDS)
    return min(max(seconds, 0.0), float(MAX_RETRY_AFTER_SECONDS))


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None and hasattr(headers, "items"):
            for key, item in headers.items():
                if str(key).lower() == name.lower():
                    value = item
                    break
        if value is None:
            return None
        return str(value)
    return None


def _cloudflare_request_id(headers: Any) -> str | None:
    for name in ("cf-ray", "CF-RAY", "cf-request-id", "x-request-id"):
        value = _header(headers, name)
        if value:
            return value
    return None


def _extract_usage(payload: dict[str, Any]) -> Any:
    for candidate in (
        payload.get("usage"),
        (payload.get("result") or {}).get("usage") if isinstance(payload.get("result"), dict) else None,
        payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
    ):
        if candidate not in (None, {}, []):
            return candidate
    return None


def _extract_neurons(usage: Any, payload: dict[str, Any]) -> Any:
    if isinstance(usage, dict) and "neurons" in usage:
        return usage.get("neurons")
    if "neurons" in payload:
        return payload.get("neurons")
    result = payload.get("result")
    if isinstance(result, dict) and "neurons" in result:
        return result.get("neurons")
    return None


def _extract_cost(usage: Any, payload: dict[str, Any]) -> float | None:
    for blob in (usage if isinstance(usage, dict) else None, payload, payload.get("result")):
        if not isinstance(blob, dict):
            continue
        for key in ("cost", "total_cost", "cost_usd", "price"):
            if key in blob and isinstance(blob[key], (int, float)):
                return float(blob[key])
    return None


def _extract_generation_ms(payload: dict[str, Any]) -> int | None:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for blob in (payload, result, payload.get("timing") if isinstance(payload.get("timing"), dict) else {}):
        if not isinstance(blob, dict):
            continue
        for key in ("generation_time_ms", "inference_time_ms", "duration_ms"):
            if key in blob and isinstance(blob[key], (int, float)):
                return int(blob[key])
    return None


def _extract_seed(payload: dict[str, Any], requested: int | None) -> int | None:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for blob in (result, payload):
        if isinstance(blob, dict) and isinstance(blob.get("seed"), int):
            return blob["seed"]
    return requested


def _extract_image_b64(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        return result
    if isinstance(result, dict):
        for key in ("image", "image_b64", "b64_json"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    for key in ("image", "image_b64"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    raise ValueError("unsupported_image_magic")


def _decode_image_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    raw_b64 = _extract_image_b64(payload)
    if not raw_b64:
        raise ValueError("missing_image")
    try:
        data = base64.b64decode(raw_b64, validate=False)
    except Exception as exc:
        raise ValueError("invalid_image_base64") from exc
    if not data:
        raise ValueError("missing_image")
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
            fmt = (image.format or "").lower()
        if width <= 0 or height <= 0:
            raise ValueError("invalid_image_bytes")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid_image_bytes") from exc
    suffix = _image_suffix(data)
    return data, fmt or suffix.lstrip(".")


def _error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                message = first.get("message") or first.get("code")
                if message:
                    return str(message)
            return str(first)
        for key in ("error", "message", "description"):
            if payload.get(key):
                return str(payload[key])
    return fallback


class CloudflareHttpResponse:
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
    data: dict[str, str],
    timeout: int,
    files: dict[str, Any] | None = None,
) -> CloudflareHttpResponse:
    multipart: dict[str, Any] = {key: (None, value) for key, value in data.items()}
    if files:
        multipart.update(files)
    response = requests.post(
        url,
        headers=headers,
        files=multipart,
        timeout=timeout,
        allow_redirects=False,
    )
    return CloudflareHttpResponse(
        status_code=response.status_code,
        content=response.content or b"",
        headers=response.headers,
        text=response.text,
    )


class CloudflareImageProvider(ImageProvider):
    # Klein 4B accepts up to four binary references as input_image_0..3.
    reference_images_supported = True
    def __init__(
        self,
        config: CloudflareConfig,
        *,
        model: str = DEFAULT_MODEL,
        transport: Callable[..., CloudflareHttpResponse] | None = None,
        sleep: Callable[[float], None] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise CloudflareConfigError("model must be a non-empty string")
        self._config = config
        self.model = model.strip()
        self._transport = transport or default_transport
        self._sleep = time.sleep if sleep is None else sleep
        self.timeout_seconds = timeout_seconds
        if max_retries < 0:
            raise CloudflareConfigError("max_retries must be >= 0")
        self.max_retries = int(max_retries)

    def secrets(self) -> tuple[str, ...]:
        return self._config.secrets()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_name="cloudflare_workers_ai",
            model_name=self.model,
            model_version=None,
            backend_type="remote_hosted",
            device=None,
            precision=None,
            quantization=None,
            license_name=None,
            commercial_use_status=None,
            license_source=None,
            deployment_notes="Cloudflare Workers AI REST /ai/run. License/commercial status unverified here.",
        )

    def wire_form(self, request: ProviderRequest) -> dict[str, str]:
        width = clamp_dimension(request.width, DEFAULT_WIDTH)
        height = clamp_dimension(request.height, DEFAULT_HEIGHT)
        prompt = request.prompt
        if request.reference_image_path:
            prompt = with_reference_prompt(prompt, role=request.reference_image_role)
        form = {
            "prompt": prompt,
            "width": str(width),
            "height": str(height),
        }
        if request.seed is not None:
            form["seed"] = str(request.seed)
        if request.guidance is not None:
            form["guidance"] = str(request.guidance)
        if request.extra.get("send_negative_prompt") and request.negative_prompt:
            form["negative_prompt"] = request.negative_prompt
        return form

    def wire_reference_files(self, request: ProviderRequest) -> dict[str, Any] | None:
        path_value = request.reference_image_path
        if not path_value:
            return None
        path = Path(path_value)
        inspected = validate_reference_image(path)
        width = int(inspected["width"])
        height = int(inspected["height"])
        if width > MAX_REFERENCE_EDGE or height > MAX_REFERENCE_EDGE:
            raise CloudflareConfigError("reference_too_large")
        mime = REFERENCE_MIME.get(str(inspected["format"]), inspected.get("mime_type") or "application/octet-stream")
        payload = path.read_bytes()
        if file_sha256(path) != inspected["sha256"]:
            raise CloudflareConfigError("reference file changed while preparing multipart")
        return {
            REFERENCE_FIELD: (path.name, payload, mime),
        }

    def recorded_request(self, request: ProviderRequest) -> dict[str, Any]:
        form = self.wire_form(request)
        attached = bool(request.reference_image_path)
        ref_meta: dict[str, Any] | None = None
        if attached:
            inspected = validate_reference_image(Path(request.reference_image_path))
            ref_meta = {
                "field": REFERENCE_FIELD,
                "filename": Path(request.reference_image_path).name,
                "sha256": inspected["sha256"],
                "width": inspected["width"],
                "height": inspected["height"],
                "mime": REFERENCE_MIME.get(str(inspected["format"]), inspected.get("mime_type")),
                "bytes_included_in_artifact": False,
            }
        payload = {
            "provider_name": "cloudflare_workers_ai",
            "model": self.model,
            "endpoint": f"{API_BASE}/accounts/{{account_id}}/ai/run/{{model}}",
            "method": "POST",
            "content_type": "multipart/form-data",
            "form": form,
            "timeout_seconds": self.timeout_seconds,
            "allow_redirects": False,
            "max_retries": self.max_retries,
            "negative_prompt_recorded": request.negative_prompt,
            "negative_prompt_sent": "negative_prompt" in form,
            "translation_applied": True,
            "translation_reason": (
                "Cloudflare FLUX.2 Klein REST uses multipart form fields; "
                "one story reference is sent as binary input_image_0 when present; "
                "negative_prompt is omitted unless explicitly enabled because it is "
                "not a documented Klein 4B parameter."
            ),
            "account_id_present": True,
            "authorization_included": False,
            "reference_images_supported": True,
            "reference_image_attached": attached,
            "reference_form_field": REFERENCE_FIELD if attached else None,
            "reference_image": ref_meta,
            "reference_required": request.reference_required,
        }
        return sanitize_cloudflare_value(payload, self.secrets())

    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        identity = self.identity()
        width = clamp_dimension(request.width, DEFAULT_WIDTH)
        height = clamp_dimension(request.height, DEFAULT_HEIGHT)
        if request.reference_required and not request.reference_image_path:
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=0,
                retry_count=0,
                http_status=None,
                request_id=None,
                reason="missing_reference",
            )
        try:
            binary_files = self.wire_reference_files(request)
        except (CloudflareConfigError, ImageValidationError) as exc:
            reason = getattr(exc, "code", None) or str(exc)
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=0,
                retry_count=0,
                http_status=None,
                request_id=None,
                reason=str(reason),
            )
        started = perf_counter()
        url = workers_ai_url(self._config.account_id, self.model)
        headers = {"Authorization": f"Bearer {self._config.api_token}"}
        form = self.wire_form(request)
        retry_count = 0
        last_status: int | None = None
        request_id: str | None = None
        payload: Any = None
        response: CloudflareHttpResponse | None = None

        try:
            while True:
                response = self._transport(
                    url,
                    headers=headers,
                    data=form,
                    timeout=self.timeout_seconds,
                    files=binary_files,
                )
                last_status = response.status_code
                request_id = _cloudflare_request_id(response.headers) or request_id
                if last_status in {429} or (last_status is not None and last_status >= 500):
                    if retry_count >= self.max_retries:
                        break
                    retry_count += 1
                    wait_s = parse_retry_after(_header(response.headers, "Retry-After"))
                    self._sleep(wait_s)
                    continue
                break
        except requests.RequestException as exc:
            elapsed = int((perf_counter() - started) * 1000)
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=elapsed,
                retry_count=retry_count,
                http_status=last_status,
                request_id=request_id,
                reason=f"transport_error: {exc.__class__.__name__}",
            )

        elapsed = int((perf_counter() - started) * 1000)
        content_type = (_header(response.headers, "Content-Type") or "").lower() if response else ""

        if last_status is not None and 300 <= last_status < 400:
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=elapsed,
                retry_count=retry_count,
                http_status=last_status,
                request_id=request_id,
                reason="unsafe_redirect",
            )

        if last_status != 200:
            try:
                payload = response.json() if response else None
            except Exception:
                payload = None
            reason = _error_message(payload, f"http_{last_status}")
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=elapsed,
                retry_count=retry_count,
                http_status=last_status,
                request_id=request_id,
                reason=reason,
            )

        if "application/json" in content_type or (response and response.content[:1] in (b"{", b"[")):
            try:
                payload = response.json()
            except Exception:
                return self._failure(
                    identity=identity,
                    dest_path=dest_path,
                    cold_start=cold_start,
                    width=width,
                    height=height,
                    elapsed=elapsed,
                    retry_count=retry_count,
                    http_status=last_status,
                    request_id=request_id,
                    reason="malformed_json",
                )
            if not isinstance(payload, dict):
                return self._failure(
                    identity=identity,
                    dest_path=dest_path,
                    cold_start=cold_start,
                    width=width,
                    height=height,
                    elapsed=elapsed,
                    retry_count=retry_count,
                    http_status=last_status,
                    request_id=request_id,
                    reason="malformed_response",
                )
            if payload.get("success") is False:
                return self._failure(
                    identity=identity,
                    dest_path=dest_path,
                    cold_start=cold_start,
                    width=width,
                    height=height,
                    elapsed=elapsed,
                    retry_count=retry_count,
                    http_status=last_status,
                    request_id=request_id,
                    reason=_error_message(payload, "cloudflare_unsuccessful"),
                )
            try:
                image_bytes, fmt = _decode_image_bytes(payload)
            except ValueError as exc:
                return self._failure(
                    identity=identity,
                    dest_path=dest_path,
                    cold_start=cold_start,
                    width=width,
                    height=height,
                    elapsed=elapsed,
                    retry_count=retry_count,
                    http_status=last_status,
                    request_id=request_id,
                    reason=str(exc),
                )
            response_format = f"json+base64/{fmt}"
        elif response and response.content:
            image_bytes = response.content
            try:
                with Image.open(BytesIO(image_bytes)) as image:
                    image.load()
                    fmt = (image.format or "png").lower()
                _image_suffix(image_bytes)
            except Exception:
                return self._failure(
                    identity=identity,
                    dest_path=dest_path,
                    cold_start=cold_start,
                    width=width,
                    height=height,
                    elapsed=elapsed,
                    retry_count=retry_count,
                    http_status=last_status,
                    request_id=request_id,
                    reason="invalid_image_bytes",
                )
            payload = {}
            response_format = fmt
        else:
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=elapsed,
                retry_count=retry_count,
                http_status=last_status,
                request_id=request_id,
                reason="missing_image",
            )

        dest = Path(dest_path)
        try:
            suffix = _image_suffix(image_bytes)
        except ValueError:
            return self._failure(
                identity=identity,
                dest_path=dest_path,
                cold_start=cold_start,
                width=width,
                height=height,
                elapsed=elapsed,
                retry_count=retry_count,
                http_status=last_status,
                request_id=request_id,
                reason="invalid_image_bytes",
            )
        if dest.suffix.lower() != suffix:
            dest = dest.with_suffix(suffix)
        refuse_overwrite(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_bytes)

        with Image.open(dest) as image:
            returned_w, returned_h = image.size

        usage = _extract_usage(payload if isinstance(payload, dict) else {})
        neurons = _extract_neurons(usage, payload if isinstance(payload, dict) else {})
        cost = _extract_cost(usage, payload if isinstance(payload, dict) else {})
        generation_ms = _extract_generation_ms(payload if isinstance(payload, dict) else {})
        seed = _extract_seed(payload if isinstance(payload, dict) else {}, request.seed)

        return ProviderResult(
            provider_name=identity.provider_name,
            success=True,
            model_name=identity.model_name,
            model_version=None,
            backend_type=identity.backend_type,
            device=None,
            load_time_ms=None,
            generation_time_ms=generation_ms,
            total_latency_ms=elapsed,
            backend_startup_ms=None,
            first_image_latency_ms=elapsed if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            width=returned_w,
            height=returned_h,
            seed=seed,
            steps=request.steps,
            guidance=request.guidance,
            retry_count=retry_count,
            failure_reason=None,
            provider_reported_cost=cost,
            actual_cost_inr=None,
            raw_image_path=str(dest),
            sha256=file_sha256(dest),
            license_name=None,
            commercial_use_status=None,
            license_source=None,
            deployment_notes=identity.deployment_notes,
            http_status=last_status,
            request_latency_ms=elapsed,
            cloudflare_request_id=request_id,
            response_format=response_format,
            size_bytes=len(image_bytes),
            provider_reported_usage=usage,
            provider_reported_neurons=neurons,
            requested_width=width,
            requested_height=height,
        )

    def _failure(
        self,
        *,
        identity: ProviderIdentity,
        dest_path: str,
        cold_start: bool,
        width: int,
        height: int,
        elapsed: int,
        retry_count: int,
        http_status: int | None,
        request_id: str | None,
        reason: str,
    ) -> ProviderResult:
        sanitized = str(sanitize_cloudflare_value(reason, self.secrets()))
        return ProviderResult(
            provider_name=identity.provider_name,
            success=False,
            model_name=identity.model_name,
            backend_type=identity.backend_type,
            load_time_ms=None,
            generation_time_ms=None,
            total_latency_ms=elapsed,
            first_image_latency_ms=elapsed if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            retry_count=retry_count,
            failure_reason=sanitized,
            provider_reported_cost=None,
            actual_cost_inr=None,
            raw_image_path=None,
            sha256=None,
            http_status=http_status,
            request_latency_ms=elapsed,
            cloudflare_request_id=request_id,
            response_format=None,
            size_bytes=None,
            provider_reported_usage=None,
            provider_reported_neurons=None,
            requested_width=width,
            requested_height=height,
        )
