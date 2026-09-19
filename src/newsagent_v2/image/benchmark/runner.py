"""
Fair image-benchmark runner.

Feeds the same visual brief to each provider. Does not call Groq, Telegram,
WordPress, or download weights. Persistence keeps raw and final separate.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from newsagent_v2.image.artifacts import (
    DEFAULT_RUNS_ROOT,
    new_run_id,
    persist_image_run,
    prepare_run_dir,
    raw_destination,
    run_dir,
    utc_now,
    final_dir,
)
from newsagent_v2.image.benchmark.scorecard import build_scorecard
from newsagent_v2.image.brief import VisualBrief
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.provider import (
    ImageProvider,
    ImageProviderError,
    ProviderRequest,
    canonical_provider_request,
)
from newsagent_v2.image.reference import apply_reference_policy, empty_reference_telemetry
from newsagent_v2.image.telemetry import telemetry_from_result
from newsagent_v2.image.validate import ImageValidationError, validate_raw_artwork


def run_provider_benchmark(
    provider: ImageProvider,
    brief: VisualBrief,
    *,
    generations: int = 1,
    persist_root: Path | None = None,
    compose: bool = False,
    composition_spec: CompositionSpec | None = None,
    now: datetime | None = None,
    request: ProviderRequest | None = None,
) -> list[dict[str, Any]]:
    if generations < 1:
        raise ValueError("generations must be >= 1")
    shared_request = request or canonical_provider_request(brief)
    try:
        reference_info = apply_reference_policy(provider, shared_request)
    except (ImageProviderError, ImageValidationError) as exc:
        code = getattr(exc, "code", "reference_error")
        raise ImageProviderError(code, str(exc)) from exc
    load_info = provider.load()
    results: list[dict[str, Any]] = []
    stamp = now or utc_now()
    warm_times: list[int] = []

    for index in range(generations):
        cold = index == 0
        run_id = new_run_id(stamp)
        base = prepare_run_dir(run_dir(run_id, root=persist_root or DEFAULT_RUNS_ROOT))
        dest = raw_destination(base)
        result = provider.generate(shared_request, dest_path=str(dest), cold_start=cold)
        if result.load_time_ms is None and cold:
            result.load_time_ms = load_info.get("load_time_ms")
        if result.backend_startup_ms is None and cold:
            result.backend_startup_ms = load_info.get("backend_startup_ms")
        validation: dict[str, Any] | None
        if result.success and (result.raw_image_path or dest.exists()):
            raw_path = Path(result.raw_image_path or dest)
            try:
                validation = validate_raw_artwork(raw_path)
                result.sha256 = validation["sha256"]
                result.width = validation["width"]
                result.height = validation["height"]
                if result.size_bytes is None:
                    result.size_bytes = validation["size_bytes"]
            except ImageValidationError as exc:
                validation = {
                    "passed": False,
                    "code": exc.code,
                    "message": exc.message,
                    "path": str(raw_path),
                    "aesthetic_score": None,
                }
        else:
            validation = None
        if result.generation_time_ms is not None and not cold:
            warm_times.append(result.generation_time_ms)

        timestamp = stamp.strftime("%Y%m%dT%H%M%SZ")
        telemetry = telemetry_from_result(
            run_id=run_id,
            event_id=brief.event_id,
            result=result,
            timestamp_utc=timestamp,
            warm_generation_times_ms=list(warm_times),
            reference=reference_info or empty_reference_telemetry(),
        )
        scorecard = build_scorecard(
            run_id=run_id,
            event_id=brief.event_id,
            provider_name=result.provider_name,
            model_name=result.model_name,
            objective={
                **result.as_dict(),
                "size_bytes": None if validation is None else validation.get("size_bytes"),
                "sha256": result.sha256,
            },
        )
        composition = None
        if compose and result.success and validation and validation.get("passed"):
            spec = composition_spec or CompositionSpec(
                headline=brief.editorial_subject,
                category_label=brief.story_category,
                test_mode=True,
            )
            composition = compose_card(
                Path(result.raw_image_path or dest),
                final_dir(base) / spec.output_name,
                spec,
            )
        stored_request = shared_request.as_dict()
        recorded = getattr(provider, "recorded_request", None)
        if callable(recorded):
            stored_request = recorded(shared_request)
        secrets = ()
        secret_fn = getattr(provider, "secrets", None)
        if callable(secret_fn):
            secrets = secret_fn()
        persist_image_run(
            base,
            brief=brief.as_dict(),
            provider_request=stored_request,
            provider_result=result.as_dict(),
            telemetry=telemetry,
            scorecard=scorecard,
            composition=composition,
            validation=validation,
            run_id=run_id,
            secrets=secrets,
        )
        results.append(
            {
                "run_id": run_id,
                "run_dir": str(base),
                "result": result.as_dict(),
                "telemetry": telemetry,
                "scorecard": scorecard,
                "request": stored_request,
                "composition": composition,
                "validation": validation,
            }
        )
    return results


def run_fair_benchmark(
    providers: Sequence[ImageProvider],
    brief: VisualBrief,
    **kwargs: Any,
) -> list[list[dict[str, Any]]]:
    request = canonical_provider_request(brief)
    return [
        run_provider_benchmark(provider, brief, request=request, **kwargs)
        for provider in providers
    ]
