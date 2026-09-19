"""Top-5 batch telemetry. Does not invent billed cost."""

from __future__ import annotations

from typing import Any

from newsagent_v2.batch.contract import BATCH_SCHEMA_VERSION, TOP5_COUNT
from newsagent_v2.benchmark.telemetry import redact_secrets


def build_batch_telemetry(
    *,
    batch_run_id: str,
    selected_stories: list[str],
    publishable_stories: list[str],
    failed_stories: list[dict[str, Any]],
    skipped_stories: list[dict[str, Any]],
    total_elapsed_ms: int,
    ai_request_count: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    image_request_count: int,
    telegram_sends: int,
    retries: int,
    provider_reported_cost_usd: float | None,
    estimated_list_price_usd: float | None,
    timestamp_utc: str,
    secrets: list[str] | tuple[str, ...] = (),
    stories: list[dict[str, Any]] | None = None,
    editorial: dict[str, Any] | None = None,
    discovery_ms: int | None = None,
    enrichment_ms: int | None = None,
    editorial_ms: int | None = None,
    qa_ms: int | None = None,
    images_ms: int | None = None,
    telegram_ms: int | None = None,
    total_timing_basis: str = "sum_of_stages",
    viability: dict[str, Any] | None = None,
    expected_count: int | None = None,
) -> dict[str, Any]:
    editorial_payload = editorial or {}
    editorial_count = int(editorial_payload.get("editorial_ai_request_count") or 0)
    payload = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_run_id": batch_run_id,
        "selected_count": len(selected_stories),
        "selected_stories": list(selected_stories),
        "publishable_stories": list(publishable_stories),
        "failed_stories": failed_stories,
        "skipped_stories": skipped_stories,
        "expected_count": (
            int(expected_count)
            if expected_count is not None
            else (int(viability["target"]) if isinstance(viability, dict) and viability.get("target") is not None else TOP5_COUNT)
        ),
        "total_elapsed_ms": total_elapsed_ms,
        "discovery_ms": discovery_ms,
        "enrichment_ms": enrichment_ms,
        "editorial_ms": editorial_ms,
        "qa_ms": qa_ms,
        "images_ms": images_ms,
        "telegram_ms": telegram_ms,
        "total_timing_basis": total_timing_basis,
        "ai_request_count": ai_request_count,
        "editorial_ai_request_count": editorial_count,
        "editorial": {
            "provider": editorial_payload.get("provider"),
            "model": editorial_payload.get("model"),
            "request_count": editorial_payload.get("request_count", editorial_count),
            "latency_ms": editorial_payload.get("latency_ms"),
            "http_status": editorial_payload.get("http_status"),
            "retries": editorial_payload.get("retries"),
            "prompt_tokens": editorial_payload.get("prompt_tokens"),
            "completion_tokens": editorial_payload.get("completion_tokens"),
            "total_tokens": editorial_payload.get("total_tokens"),
            "provider_reported_cost_usd": editorial_payload.get("provider_reported_cost_usd"),
            "estimated_list_price_usd": editorial_payload.get("estimated_list_price_usd"),
            "requested_event_ids": editorial_payload.get("requested_event_ids") or [],
            "provider_error": editorial_payload.get("provider_error"),
            "request_diagnostics": editorial_payload.get("request_diagnostics") or {},
            "retry_after": editorial_payload.get("retry_after"),
            "retry_slept_seconds": editorial_payload.get("retry_slept_seconds"),
            "attempts": editorial_payload.get("attempts"),
        },
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "image_request_count": image_request_count,
        "telegram_sends": telegram_sends,
        "retries": retries,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "estimated_list_price_usd": estimated_list_price_usd,
        "timestamp_utc": timestamp_utc,
        "stories": stories or [],
        "viability": viability or {},
    }
    return redact_secrets(payload, secrets)
