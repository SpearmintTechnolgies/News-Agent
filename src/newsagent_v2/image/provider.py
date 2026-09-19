"""
Provider-independent image backend interface.

Unknown telemetry is null. Implementations must not invent cost, license,
VRAM, or quality. Phase A includes no downloading backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.image.brief import VisualBrief
from newsagent_v2.image.contract import (
    BACKEND_TYPES,
    DEFAULT_REFERENCE_IMAGE_ROLE,
    IMAGE_PROVIDER_RESULT_SCHEMA_VERSION,
    normalize_reference_role,
)


class ImageProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderIdentity:
    provider_name: str
    model_name: str | None = None
    model_version: str | None = None
    backend_type: str | None = None
    device: str | None = None
    precision: str | None = None
    quantization: str | None = None
    license_name: str | None = None
    commercial_use_status: str | None = None
    license_source: str | None = None
    deployment_notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "backend_type": self.backend_type,
            "device": self.device,
            "precision": self.precision,
            "quantization": self.quantization,
            "license_name": self.license_name,
            "commercial_use_status": self.commercial_use_status,
            "license_source": self.license_source,
            "deployment_notes": self.deployment_notes,
        }


@dataclass
class ProviderRequest:
    prompt: str
    negative_prompt: str | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    steps: int | None = None
    guidance: float | None = None
    translation_applied: bool = False
    translation_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    reference_image_path: str | None = None
    reference_image_role: str | None = None
    reference_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "steps": self.steps,
            "guidance": self.guidance,
            "translation_applied": self.translation_applied,
            "translation_reason": self.translation_reason,
            "extra": dict(self.extra),
            "reference_image_path": self.reference_image_path,
            "reference_image_role": self.reference_image_role,
            "reference_required": self.reference_required,
        }


@dataclass
class ProviderResult:
    provider_name: str
    success: bool
    schema_version: str = IMAGE_PROVIDER_RESULT_SCHEMA_VERSION
    model_name: str | None = None
    model_version: str | None = None
    backend_type: str | None = None
    device: str | None = None
    precision: str | None = None
    quantization: str | None = None
    load_time_ms: int | None = None
    generation_time_ms: int | None = None
    total_latency_ms: int | None = None
    backend_startup_ms: int | None = None
    first_image_latency_ms: int | None = None
    cold_start: bool | None = None
    warm_generation: bool | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    steps: int | None = None
    guidance: float | None = None
    peak_memory_mb: float | None = None
    peak_vram_mb: float | None = None
    retry_count: int = 0
    failure_reason: str | None = None
    provider_reported_cost: float | None = None
    actual_cost_inr: float | None = None
    raw_image_path: str | None = None
    sha256: str | None = None
    license_name: str | None = None
    commercial_use_status: str | None = None
    license_source: str | None = None
    deployment_notes: str | None = None
    http_status: int | None = None
    request_latency_ms: int | None = None
    cloudflare_request_id: str | None = None
    response_format: str | None = None
    size_bytes: int | None = None
    provider_reported_usage: Any = None
    provider_reported_neurons: Any = None
    requested_width: int | None = None
    requested_height: int | None = None
    provider_reported_cost_usd: float | None = None
    provider_reported_image_usage: Any = None
    provider_reported_tokens: Any = None
    provider_reported_latency: int | None = None
    estimated_list_price_usd: float | None = None
    estimated_list_price_inr: float | None = None
    estimated_list_price_is_estimate: bool | None = None
    estimated_list_price_note: str | None = None
    requested_aspect_ratio: str | None = None
    requested_resolution_tier: str | None = None
    provider_request_id: str | None = None
    total_pipeline_latency_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "backend_type": self.backend_type,
            "device": self.device,
            "precision": self.precision,
            "quantization": self.quantization,
            "load_time_ms": self.load_time_ms,
            "generation_time_ms": self.generation_time_ms,
            "total_latency_ms": self.total_latency_ms,
            "backend_startup_ms": self.backend_startup_ms,
            "first_image_latency_ms": self.first_image_latency_ms,
            "cold_start": self.cold_start,
            "warm_generation": self.warm_generation,
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "steps": self.steps,
            "guidance": self.guidance,
            "peak_memory_mb": self.peak_memory_mb,
            "peak_vram_mb": self.peak_vram_mb,
            "retry_count": self.retry_count,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "provider_reported_cost": self.provider_reported_cost,
            "actual_cost_inr": self.actual_cost_inr,
            "raw_image_path": self.raw_image_path,
            "sha256": self.sha256,
            "license_name": self.license_name,
            "commercial_use_status": self.commercial_use_status,
            "license_source": self.license_source,
            "deployment_notes": self.deployment_notes,
            "http_status": self.http_status,
            "request_latency_ms": self.request_latency_ms,
            "cloudflare_request_id": self.cloudflare_request_id,
            "response_format": self.response_format,
            "size_bytes": self.size_bytes,
            "provider_reported_usage": self.provider_reported_usage,
            "provider_reported_neurons": self.provider_reported_neurons,
            "requested_width": self.requested_width,
            "requested_height": self.requested_height,
            "provider_reported_cost_usd": self.provider_reported_cost_usd,
            "provider_reported_image_usage": self.provider_reported_image_usage,
            "provider_reported_tokens": self.provider_reported_tokens,
            "provider_reported_latency": self.provider_reported_latency,
            "estimated_list_price_usd": self.estimated_list_price_usd,
            "estimated_list_price_inr": self.estimated_list_price_inr,
            "estimated_list_price_is_estimate": self.estimated_list_price_is_estimate,
            "estimated_list_price_note": self.estimated_list_price_note,
            "requested_aspect_ratio": self.requested_aspect_ratio,
            "requested_resolution_tier": self.requested_resolution_tier,
            "provider_request_id": self.provider_request_id,
            "total_pipeline_latency_ms": self.total_pipeline_latency_ms,
        }


def canonical_provider_request(
    brief: VisualBrief,
    *,
    reference_image_path: str | None = None,
    reference_image_role: str | None = None,
    reference_required: bool = False,
) -> ProviderRequest:
    """Identical semantic request for every backend before optional syntax translation."""
    role = None
    if reference_image_path or reference_image_role:
        try:
            role = normalize_reference_role(reference_image_role or DEFAULT_REFERENCE_IMAGE_ROLE)
        except ValueError as exc:
            raise ImageProviderError("invalid_reference_role", str(exc)) from exc
    return ProviderRequest(
        prompt=brief.artwork_prompt(),
        negative_prompt=brief.negative_prompt,
        width=brief.width,
        height=brief.height,
        translation_applied=False,
        translation_reason=None,
        reference_image_path=reference_image_path,
        reference_image_role=role,
        reference_required=reference_required,
    )


def apply_syntax_translation(
    request: ProviderRequest,
    *,
    extra: dict[str, Any] | None = None,
    reason: str,
) -> ProviderRequest:
    """Record a technical syntax adapter. Do not use this to improve one model's prompt."""
    if not reason.strip():
        raise ImageProviderError("translation_reason", "syntax translation requires a reason")
    merged = dict(request.extra)
    if extra:
        merged.update(extra)
    return ProviderRequest(
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        width=request.width,
        height=request.height,
        seed=request.seed,
        steps=request.steps,
        guidance=request.guidance,
        translation_applied=True,
        translation_reason=reason.strip(),
        extra=merged,
        reference_image_path=request.reference_image_path,
        reference_image_role=request.reference_image_role,
        reference_required=request.reference_required,
    )


class ImageProvider(ABC):
    reference_images_supported: bool = False

    @abstractmethod
    def identity(self) -> ProviderIdentity:
        raise NotImplementedError

    def load(self) -> dict[str, Any]:
        """Load weights or connect to a local backend. Default: already loaded."""
        return {
            "load_time_ms": 0,
            "backend_startup_ms": None,
            "loaded": True,
        }

    @abstractmethod
    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        raise NotImplementedError

    def shutdown(self) -> None:
        return None


def validate_identity(identity: ProviderIdentity) -> None:
    if identity.backend_type is not None and identity.backend_type not in BACKEND_TYPES:
        raise ImageProviderError(
            "backend_type",
            f"unsupported backend_type {identity.backend_type!r}",
        )
