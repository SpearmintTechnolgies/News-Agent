"""Offline synthetic image provider. Test/harness only. Not a quality backend."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter

from newsagent_v2.image.provider import (
    ImageProvider,
    ProviderIdentity,
    ProviderRequest,
    ProviderResult,
)
from newsagent_v2.image.validate import file_sha256, refuse_overwrite, write_png_rgb


class SyntheticImageProvider(ImageProvider):
    """Writes a solid-color PNG locally. No network, no weights, no cost."""

    reference_images_supported = False

    def __init__(self, *, color: tuple[int, int, int] = (28, 36, 52), load_ms_floor: int = 0) -> None:
        self.color = color
        self._loaded = False
        self._load_time_ms = load_ms_floor

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_name="synthetic_offline",
            model_name="synthetic-solid-png",
            model_version=None,
            backend_type="synthetic_offline",
            device="cpu",
            precision=None,
            quantization=None,
            license_name=None,
            commercial_use_status=None,
            license_source=None,
            deployment_notes="Offline fixture generator for architecture tests. Not a candidate model.",
        )

    def load(self) -> dict:
        started = perf_counter()
        self._loaded = True
        elapsed = max(self._load_time_ms, int((perf_counter() - started) * 1000))
        self._load_time_ms = elapsed
        return {
            "load_time_ms": elapsed,
            "backend_startup_ms": elapsed,
            "loaded": True,
        }

    def generate(self, request: ProviderRequest, *, dest_path: str, cold_start: bool) -> ProviderResult:
        identity = self.identity()
        if request.reference_required:
            return ProviderResult(
                provider_name=identity.provider_name,
                success=False,
                model_name=identity.model_name,
                backend_type=identity.backend_type,
                failure_reason="reference_unsupported",
                retry_count=0,
                provider_reported_cost=None,
                actual_cost_inr=None,
            )
        started = perf_counter()
        load_time = self._load_time_ms if self._loaded else None
        dest = Path(dest_path)
        refuse_overwrite(dest)
        width = int(request.width or 1280)
        height = int(request.height or 720)
        write_png_rgb(dest, width, height, self.color)
        generation_ms = int((perf_counter() - started) * 1000)
        total = (load_time or 0) + generation_ms if cold_start else generation_ms
        return ProviderResult(
            provider_name=identity.provider_name,
            success=True,
            model_name=identity.model_name,
            model_version=identity.model_version,
            backend_type=identity.backend_type,
            device=identity.device,
            precision=None,
            quantization=None,
            load_time_ms=load_time,
            generation_time_ms=generation_ms,
            total_latency_ms=total,
            backend_startup_ms=load_time if cold_start else None,
            first_image_latency_ms=total if cold_start else None,
            cold_start=cold_start,
            warm_generation=not cold_start,
            width=width,
            height=height,
            seed=request.seed,
            steps=request.steps,
            guidance=request.guidance,
            peak_memory_mb=None,
            peak_vram_mb=None,
            retry_count=0,
            failure_reason=None,
            provider_reported_cost=None,
            actual_cost_inr=None,
            raw_image_path=str(dest),
            sha256=file_sha256(dest),
            license_name=None,
            commercial_use_status=None,
            license_source=None,
            deployment_notes=identity.deployment_notes,
        )
