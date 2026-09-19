"""
Vertex AI Nano Banana image provider.

Uses Vertex generateContent for Gemini image models. Does not use the
Generative Language API key path (that remains GeminiImageProvider).

Never logs or persists credentials, tokens, or service-account JSON.
No automatic retries. Hard generation call cap is enforced by callers.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

import requests
from PIL import Image

from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.image.provider import (
    ImageProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
)
from newsagent_v2.image.providers.gemini import (
    REQUESTED_ASPECT_RATIO,
    REQUESTED_RESOLUTION_TIER,
    _first_inline_image,
    _header,
    _image_suffix,
    _usage_blobs,
)
from newsagent_v2.image.reference import with_reference_prompt
from newsagent_v2.image.validate import file_sha256, refuse_overwrite, validate_reference_image

PROJECT_ENV = "NEWSAGENT_V2_VERTEX_PROJECT"
LOCATION_ENV = "NEWSAGENT_V2_VERTEX_LOCATION"
MODEL_ENV = "NEWSAGENT_V2_VERTEX_MODEL"
MODEL_ALIAS_ENV = "NEWSAGENT_V2_NANO_BANANA_MODEL"
CREDENTIALS_PATH_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
# Non-secret fallbacks commonly used by Google client libraries
PROJECT_FALLBACKS = ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT")
LOCATION_FALLBACKS = ("GOOGLE_CLOUD_LOCATION", "GOOGLE_CLOUD_REGION", "VERTEX_LOCATION")

DEFAULT_TIMEOUT_SECONDS = 120
PROVIDER_NAME = "vertex"
# No default model — must come from explicit env when live.
HARD_MAX_GENERATION_CALLS = 1


class VertexConfigError(ValueError):
    """Missing or invalid Vertex configuration. Never includes secret values."""


@dataclass(frozen=True)
class VertexNanoBananaConfig:
    project: str
    location: str
    model: str
    credentials_path_set: bool
    auth_mode: str  # "adc" | "service_account_file" | "access_token_injection"

    def secrets(self) -> tuple[str, ...]:
        # Tokens are obtained at call time; nothing stored here is a secret blob.
        return ()


def _nonempty(environ: dict[str, str], key: str) -> str | None:
    raw = environ.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def resolve_vertex_config_status(environ: dict[str, str] | None) -> dict[str, Any]:
    """
    Report NON-SECRET configuration + local auth readiness.

    Ready requires:
      - project / location / model configured
      - credential file exists + readable (when using SA file)
      - process os.environ synchronized for GOOGLE_APPLICATION_CREDENTIALS
      - google.auth.default() resolves credentials locally (no generateContent)
    Never returns credential values, tokens, or private keys.
    Does not refresh credentials (avoids network token exchange).
    """
    if environ is None:
        return {
            "ready": False,
            "missing_fields": ["environ"],
            "project_configured": False,
            "location_configured": False,
            "model_configured": False,
            "credentials_path_configured": False,
            "credentials_path_exists": False,
            "credentials_path_readable": False,
            "process_credentials_present": False,
            "adc_available": False,
            "google_auth_default_resolves": False,
            "resolved_project_matches": False,
            "credential_type": None,
            "google_auth_package_present": False,
            "provider": PROVIDER_NAME,
            "model": None,
            "region": None,
            "project_id_echo": False,
        }

    # ADC reads process env — sync application dict first.
    from newsagent_v2.image.vertex_local_env import sync_vertex_process_environ

    process_present = sync_vertex_process_environ(environ)

    project = _nonempty(environ, PROJECT_ENV)
    for key in PROJECT_FALLBACKS:
        if project:
            break
        project = _nonempty(environ, key)

    location = _nonempty(environ, LOCATION_ENV)
    for key in LOCATION_FALLBACKS:
        if location:
            break
        location = _nonempty(environ, key)

    model = _nonempty(environ, MODEL_ENV) or _nonempty(environ, MODEL_ALIAS_ENV)
    creds_path = _nonempty(environ, CREDENTIALS_PATH_ENV) or str(
        os.environ.get(CREDENTIALS_PATH_ENV, "") or ""
    ).strip() or None
    process_creds = str(os.environ.get(CREDENTIALS_PATH_ENV, "") or "").strip()
    process_credentials_present = bool(process_creds)
    creds_path_exists = bool(creds_path and Path(creds_path).is_file())
    creds_path_readable = False
    if creds_path_exists and creds_path:
        try:
            with open(creds_path, "rb") as fh:
                creds_path_readable = len(fh.read(16)) > 0
        except OSError:
            creds_path_readable = False

    google_auth_present = False
    adc_available = False
    google_auth_default_resolves = False
    resolved_project_matches = False
    credential_type: str | None = None
    resolved_project: str | None = None
    try:
        import google.auth  # noqa: F401

        google_auth_present = True
        try:
            from google.auth import default as google_auth_default

            # Local discovery only — do not refresh (refresh may hit the network).
            _creds, _proj = google_auth_default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            adc_available = _creds is not None
            google_auth_default_resolves = _creds is not None
            if _creds is not None:
                credential_type = type(_creds).__name__
            if isinstance(_proj, str) and _proj.strip():
                resolved_project = _proj.strip()
                if not project:
                    project = resolved_project
            if project and resolved_project:
                resolved_project_matches = project == resolved_project
            elif project and google_auth_default_resolves and not resolved_project:
                # SA file auth often returns project=None from default(); configured
                # project is still authoritative when credentials resolve.
                resolved_project_matches = True
            elif google_auth_default_resolves and project:
                resolved_project_matches = True
        except Exception:
            adc_available = False
            google_auth_default_resolves = False
    except Exception:
        google_auth_present = False

    missing: list[str] = []
    if not project:
        missing.append(PROJECT_ENV)
        missing.extend(list(PROJECT_FALLBACKS))
    if not location:
        missing.append(LOCATION_ENV)
        missing.extend(list(LOCATION_FALLBACKS))
    if not model:
        missing.append(MODEL_ENV)
        missing.append(MODEL_ALIAS_ENV)
    if not google_auth_present:
        missing.append("python_package:google-auth")
    if not process_credentials_present and not (creds_path_exists and google_auth_default_resolves):
        missing.append("process_env:GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path and not creds_path_exists:
        missing.append(CREDENTIALS_PATH_ENV)
    if creds_path_exists and not creds_path_readable:
        missing.append("credential_file_unreadable")
    # File-on-disk alone is insufficient — ADC must resolve via process env.
    if not google_auth_default_resolves:
        missing.append("application_default_credentials")
        missing.append("vertex_auth_not_ready")
    if project and google_auth_default_resolves and not resolved_project_matches:
        missing.append("resolved_project_mismatch")

    # Deduplicate while preserving order
    seen: set[str] = set()
    missing_unique: list[str] = []
    for item in missing:
        if item not in seen:
            seen.add(item)
            missing_unique.append(item)

    ready = len(missing_unique) == 0
    return {
        "ready": ready,
        "missing_fields": missing_unique,
        "project_configured": bool(project),
        "location_configured": bool(location),
        "model_configured": bool(model),
        "credentials_path_configured": bool(creds_path),
        "credentials_path_exists": creds_path_exists,
        "credentials_path_readable": creds_path_readable,
        "process_credentials_present": process_credentials_present,
        "process_env_synced": bool(process_present.get(CREDENTIALS_PATH_ENV)),
        "adc_available": adc_available,
        "google_auth_default_resolves": google_auth_default_resolves,
        "resolved_project_matches": resolved_project_matches,
        "credential_type": credential_type,
        "google_auth_package_present": google_auth_present,
        "provider": PROVIDER_NAME,
        "model": model,
        "region": location,
        # Never echo project ID — only boolean readiness.
        "project_id_echo": False,
    }


def load_vertex_nano_banana_config(environ: dict[str, str] | None) -> VertexNanoBananaConfig:
    status = resolve_vertex_config_status(environ)
    if not status["ready"]:
        raise VertexConfigError(
            "Vertex Nano Banana configuration incomplete. Missing non-secret fields: "
            + ", ".join(status["missing_fields"])
        )
    assert environ is not None
    project = _nonempty(environ, PROJECT_ENV)
    for key in PROJECT_FALLBACKS:
        if project:
            break
        project = _nonempty(environ, key)
    location = _nonempty(environ, LOCATION_ENV)
    for key in LOCATION_FALLBACKS:
        if location:
            break
        location = _nonempty(environ, key)
    model = _nonempty(environ, MODEL_ENV) or _nonempty(environ, MODEL_ALIAS_ENV)
    assert project and location and model
    creds_path = _nonempty(environ, CREDENTIALS_PATH_ENV)
    auth_mode = "service_account_file" if creds_path else "adc"
    return VertexNanoBananaConfig(
        project=project,
        location=location,
        model=model,
        credentials_path_set=bool(creds_path),
        auth_mode=auth_mode,
    )


def vertex_api_host(location: str) -> str:
    """
    Vertex regional hosts are `{region}-aiplatform.googleapis.com`.
    The global location must use the undprefixed host `aiplatform.googleapis.com`
    (NOT `global-aiplatform.googleapis.com`).
    """
    loc = str(location or "").strip()
    if not loc:
        raise VertexConfigError("location is required for Vertex endpoint")
    if loc.lower() == "global":
        return "aiplatform.googleapis.com"
    return f"{loc}-aiplatform.googleapis.com"


def vertex_generate_content_url(*, project: str, location: str, model: str) -> str:
    host = vertex_api_host(location)
    return (
        f"https://{host}/v1/"
        f"projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
    )


def _content_type(headers: Any) -> str | None:
    return _header(headers, "Content-Type") or _header(headers, "content-type")


def _safe_body_preview(text: str, *, limit: int = 240) -> str:
    """Truncate body for diagnostics. Strip obvious credential-like substrings."""
    cleaned = str(text or "")
    cleaned = cleaned.replace("\r", " ").replace("\n", " ")
    # Never keep long base64 blobs in diagnostics.
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    lowered = cleaned.lower()
    for marker in ("bearer ", "ya29.", "private_key", "begin private"):
        if marker in lowered:
            return "[redacted preview]"
    return cleaned


def classify_vertex_http_error(status: int | None, payload: dict[str, Any] | None) -> str:
    """Map HTTP/JSON error into a specific Vertex failure code."""
    message = ""
    status_name = ""
    code = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "")
            status_name = str(err.get("status") or "")
            code = err.get("code")
        elif payload.get("message"):
            message = str(payload.get("message") or "")
    blob = f"{status_name} {message}".lower()

    if status == 401 or "unauthenticated" in blob:
        return "vertex_auth_error"
    if status == 403 or "permission_denied" in blob or "permission denied" in blob:
        if "api" in blob and ("enable" in blob or "not been used" in blob or "disabled" in blob):
            return "vertex_api_not_enabled"
        if "model" in blob and ("access" in blob or "not allow" in blob or "denied" in blob):
            return "vertex_model_access_denied"
        return "vertex_permission_denied"
    if status == 404:
        if "model" in blob or "not found" in blob:
            return "vertex_model_not_found"
        return "vertex_http_error"
    if status == 429 or "resource_exhausted" in blob or "rate" in blob:
        return "vertex_rate_limited"
    if isinstance(code, int) and code == 404:
        return "vertex_model_not_found"
    if status is not None and status >= 400:
        return "vertex_http_error"
    return "vertex_http_error"


def classify_vertex_payload_without_image(payload: dict[str, Any]) -> str:
    """Distinguish safety / text-only / missing-image when HTTP succeeded."""
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        prompt_feedback = payload.get("promptFeedback") or payload.get("prompt_feedback")
        if isinstance(prompt_feedback, dict):
            block = str(
                prompt_feedback.get("blockReason")
                or prompt_feedback.get("block_reason")
                or ""
            ).upper()
            if block:
                return "vertex_safety_block"
        return "vertex_missing_image"

    first = candidates[0] if isinstance(candidates[0], dict) else {}
    finish = str(first.get("finishReason") or first.get("finish_reason") or "").upper()
    if finish in {"SAFETY", "BLOCKED", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "NO_IMAGE"}:
        return "vertex_safety_block"

    content = first.get("content") if isinstance(first.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    has_text = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str) and part.get("text").strip():
            has_text = True
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            return "vertex_missing_image"  # extraction helper failed elsewhere
    if has_text:
        return "vertex_text_only_response"
    return "vertex_missing_image"


def parse_vertex_response_payload(
    response: VertexHttpResponse,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """
    Parse Vertex HTTP body safely.

    Returns (payload_or_none, failure_code_or_none, safe_diagnostic).
    failure_code is set when the body cannot be used as a JSON object payload.
    """
    content_type = _content_type(response.headers)
    raw = response.content if isinstance(response.content, (bytes, bytearray)) else b""
    text = response.text if response.text is not None else ""
    diagnostic: dict[str, Any] = {
        "http_status": response.status_code,
        "response_content_type": content_type,
        "response_body_present": bool(raw) or bool(text.strip()),
        "response_body_length": len(raw) if raw else len(text.encode("utf-8", errors="replace")),
        "provider_response_class": None,
        "top_level_keys": None,
        "safe_error_status": None,
        "safe_error_code": None,
        "safe_error_message": None,
        "body_preview": None,
    }

    if not raw and not str(text).strip():
        diagnostic["provider_response_class"] = "EMPTY"
        return None, "vertex_non_json_response", diagnostic

    try:
        payload = response.json()
    except Exception:
        diagnostic["provider_response_class"] = "NON_JSON"
        diagnostic["body_preview"] = _safe_body_preview(text)
        # Only call this malformed_json when body looks like truncated/broken JSON.
        stripped = str(text).lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return None, "vertex_malformed_json", diagnostic
        return None, "vertex_non_json_response", diagnostic

    if not isinstance(payload, dict):
        diagnostic["provider_response_class"] = "VALID_JSON_NON_OBJECT"
        diagnostic["body_preview"] = _safe_body_preview(text)
        return None, "vertex_malformed_json", diagnostic

    diagnostic["provider_response_class"] = "VALID_JSON"
    diagnostic["top_level_keys"] = sorted(str(k) for k in payload.keys())
    err = payload.get("error")
    if isinstance(err, dict):
        diagnostic["safe_error_status"] = err.get("status")
        diagnostic["safe_error_code"] = err.get("code")
        msg = err.get("message")
        if isinstance(msg, str):
            diagnostic["safe_error_message"] = _safe_body_preview(msg, limit=320)
    return payload, None, diagnostic


class VertexHttpResponse:
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
) -> VertexHttpResponse:
    response = requests.post(
        url,
        headers=headers,
        json=json_body,
        timeout=timeout,
        allow_redirects=False,
    )
    return VertexHttpResponse(
        status_code=response.status_code,
        content=response.content or b"",
        headers=response.headers,
        text=response.text,
    )


def _obtain_access_token() -> str:
    try:
        from google.auth import default as google_auth_default
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except Exception as exc:
        raise VertexConfigError("python_package:google-auth is required for Vertex auth") from exc
    credentials, _project = google_auth_default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if credentials is None:
        raise VertexConfigError("application_default_credentials unavailable")
    if not getattr(credentials, "valid", False):
        credentials.refresh(GoogleAuthRequest())
    token = getattr(credentials, "token", None)
    if not isinstance(token, str) or not token.strip():
        raise VertexConfigError("failed to obtain Vertex access token")
    return token


class VertexNanoBananaImageProvider(ImageProvider):
    """Vertex AI image generation (Nano Banana / Gemini image models)."""

    reference_images_supported = True

    def __init__(
        self,
        config: VertexNanoBananaConfig,
        *,
        transport: Callable[..., VertexHttpResponse] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        access_token_provider: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self.model = config.model
        self.location = config.location
        self._transport = transport or default_transport
        self.timeout_seconds = timeout_seconds
        self._access_token_provider = access_token_provider or _obtain_access_token
        self.generation_calls = 0

    def secrets(self) -> tuple[str, ...]:
        return self._config.secrets()

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_name=PROVIDER_NAME,
            model_name=self.model,
            model_version=None,
            backend_type="remote_hosted",
            license_name=None,
            commercial_use_status=None,
            license_source=None,
            deployment_notes=(
                f"Vertex AI generateContent image API in region={self.location}. "
                "CoinNetwork logo must be applied by Python compositor only."
            ),
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
            "provider_name": PROVIDER_NAME,
            "model": self.model,
            "region": self.location,
            "project_configured": True,
            "api_host": vertex_api_host(self.location),
            "endpoint": vertex_generate_content_url(
                project=self._config.project,
                location=self._config.location,
                model=self.model,
            ).replace(self._config.project, "{project}"),
            "endpoint_template": (
                "https://{host}/v1/projects/{project}/locations/{location}/"
                "publishers/google/models/{model}:generateContent"
            ),
            "host_rule": (
                "location=global → aiplatform.googleapis.com; "
                "regional → {location}-aiplatform.googleapis.com"
            ),
            "method": "POST",
            "auth": "Bearer access token (ADC / service account)",
            "authorization_included": False,
            "api_key_in_url": False,
            "allow_redirects": False,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": 0,
            "hard_max_generation_calls": HARD_MAX_GENERATION_CALLS,
            "body": recorded_body,
            "requested_aspect_ratio": REQUESTED_ASPECT_RATIO,
            "requested_resolution_tier": REQUESTED_RESOLUTION_TIER,
            "translation_applied": True,
            "translation_reason": (
                "Vertex Nano Banana image generateContent uses aspectRatio/imageSize "
                "rather than pixel width/height."
            ),
            "reference_images_supported": True,
            "reference_image_attached": bool(request.reference_image_path),
            "reference_image_role": request.reference_image_role,
            "reference_required": request.reference_required,
        }
        return redact_secrets(payload, self.secrets())

    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        if self.generation_calls >= HARD_MAX_GENERATION_CALLS:
            identity = self.identity()
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=0,
                http_status=None,
                request_id=None,
                reason="hard_max_generation_calls_exceeded",
            )
        self.generation_calls += 1

        identity = self.identity()
        started = perf_counter()
        url = vertex_generate_content_url(
            project=self._config.project,
            location=self._config.location,
            model=self.model,
        )
        try:
            token = self._access_token_provider()
        except VertexConfigError as exc:
            elapsed = int((perf_counter() - started) * 1000)
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=None,
                request_id=None,
                reason="vertex_auth_error",
                diagnostic={"safe_error_message": str(exc)},
            )
        except Exception as exc:
            elapsed = int((perf_counter() - started) * 1000)
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=None,
                request_id=None,
                reason="vertex_auth_error",
                diagnostic={"safe_error_message": exc.__class__.__name__},
            )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
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
                response_format=_content_type(response.headers),
                diagnostic={
                    "http_status": status,
                    "provider_response_class": "REDIRECT",
                    "response_content_type": _content_type(response.headers),
                },
            )

        payload, parse_failure, diagnostic = parse_vertex_response_payload(response)
        content_type = diagnostic.get("response_content_type")

        if parse_failure is not None:
            # Non-JSON / empty / broken JSON — do not collapse HTTP semantics into malformed_json.
            reason = parse_failure
            if parse_failure == "vertex_non_json_response" and status is not None and status >= 400:
                # Prefer HTTP taxonomy when the body is not JSON (e.g. HTML 404 from wrong host).
                reason = classify_vertex_http_error(status, None)
                if status == 404:
                    # Wrong global host historically returned HTML 404; still report http class.
                    reason = "vertex_http_error"
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason=reason,
                response_format=content_type if isinstance(content_type, str) else None,
                diagnostic=diagnostic,
            )

        assert isinstance(payload, dict)
        request_id = payload.get("responseId") or payload.get("response_id") or request_id
        usage, image_usage, tokens = _usage_blobs(payload)
        if usage is not None or diagnostic:
            # Keep usage separate; attach safe diagnostic alongside when present.
            if isinstance(usage, dict):
                usage = {**usage, "_safe_response_diagnostic": diagnostic}
            else:
                usage = {"_safe_response_diagnostic": diagnostic}

        if status != 200 or isinstance(payload.get("error"), dict):
            reason = classify_vertex_http_error(status, payload)
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason=reason,
                usage=usage,
                image_usage=image_usage,
                tokens=tokens,
                response_format=content_type if isinstance(content_type, str) else None,
                diagnostic=diagnostic,
            )

        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                finish = str(first.get("finishReason") or first.get("finish_reason") or "")
                if finish.upper() in {
                    "SAFETY",
                    "BLOCKED",
                    "PROHIBITED_CONTENT",
                    "IMAGE_SAFETY",
                    "NO_IMAGE",
                }:
                    return self._failure(
                        identity=identity,
                        cold_start=cold_start,
                        elapsed=elapsed,
                        http_status=status,
                        request_id=request_id,
                        reason="vertex_safety_block",
                        usage=usage,
                        image_usage=image_usage,
                        tokens=tokens,
                        response_format=content_type if isinstance(content_type, str) else None,
                        diagnostic=diagnostic,
                    )

        extracted = _first_inline_image(payload)
        if extracted is None:
            reason = classify_vertex_payload_without_image(payload)
            return self._failure(
                identity=identity,
                cold_start=cold_start,
                elapsed=elapsed,
                http_status=status,
                request_id=request_id,
                reason=reason,
                usage=usage,
                image_usage=image_usage,
                tokens=tokens,
                response_format=content_type if isinstance(content_type, str) else None,
                diagnostic=diagnostic,
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
                response_format=mime,
                diagnostic=diagnostic,
            )

        dest = Path(dest_path)
        suffix = _image_suffix(image_bytes, mime)
        if dest.suffix.lower() != suffix:
            dest = dest.with_suffix(suffix)
        refuse_overwrite(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(image_bytes)

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
            estimated_list_price_usd=None,
            estimated_list_price_inr=None,
            estimated_list_price_is_estimate=None,
            estimated_list_price_note=None,
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
        response_format: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> ProviderResult:
        reported_usage = usage
        if diagnostic is not None:
            if isinstance(reported_usage, dict):
                reported_usage = {**reported_usage, "_safe_response_diagnostic": diagnostic}
            else:
                reported_usage = {"_safe_response_diagnostic": diagnostic}
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
            response_format=response_format,
            provider_reported_usage=reported_usage,
            provider_reported_image_usage=image_usage,
            provider_reported_tokens=tokens,
            provider_request_id=str(request_id) if request_id else None,
            requested_aspect_ratio=REQUESTED_ASPECT_RATIO,
            requested_resolution_tier=REQUESTED_RESOLUTION_TIER,
            deployment_notes=identity.deployment_notes,
        )
