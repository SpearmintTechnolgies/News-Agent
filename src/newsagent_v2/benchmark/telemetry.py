"""
Benchmark-run telemetry. Never stores API keys or Authorization headers.

Cost fields are diagnostic. provider_reported_cost_usd is null unless the
provider explicitly returns a billed amount. List-price figures are estimates
only and must not be treated as an invoice or as free-tier billed cost.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Diagnostic FX only. Not a billed conversion rate.
ESTIMATE_USD_TO_INR = 83.0

# Diagnostic Groq list prices (USD per 1 million tokens). May be stale.
# These are NOT billed cost and MUST NOT be reported as actual spend.
LIST_PRICE_USD_PER_MILLION_TOKENS: dict[str, dict[str, float]] = {
    "openai/gpt-oss-120b": {
        "input": 0.15,
        "output": 0.60,
    },
}


def redact_secrets(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    cleaned = deepcopy(value)
    blocked = {s for s in secrets if s}
    return _redact(cleaned, blocked)


def _redact(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {
                "authorization",
                "api_key",
                "groq_api_key",
                "api_token",
                "gemini_api_key",
                "x-goog-api-key",
                "bedrock_mantle_api_key",
                "newsagent_v2_bedrock_mantle_api_key",
                "x-api-key",
                "bearer",
            }:
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact(item, secrets)
        return out
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret and secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text
    return value


def estimated_list_price_usd(
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> float | None:
    prices = LIST_PRICE_USD_PER_MILLION_TOKENS.get(model)
    if prices is None:
        return None
    if prompt_tokens is None and completion_tokens is None:
        return None
    usd = 0.0
    if prompt_tokens is not None:
        usd += (prompt_tokens / 1_000_000) * prices["input"]
    if completion_tokens is not None:
        usd += (completion_tokens / 1_000_000) * prices["output"]
    return round(usd, 8)


def estimated_list_price_inr(usd: float | None) -> float | None:
    if usd is None:
        return None
    return round(usd * ESTIMATE_USD_TO_INR, 6)


def build_telemetry(
    *,
    schema_version: str,
    input_schema_version: str,
    provider: str,
    model: str,
    reasoning_effort: str,
    timestamp_utc: str,
    latency_ms: int | None,
    http_status: int | None,
    retry_count: int,
    success: bool,
    failure_reason: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    provider_request_id: str | None,
    validator_passed: bool | None,
    validator_error_count: int | None,
    validator_errors: list[str] | None,
    selected_event_ids: list[str] | None,
    raw_response_path: str | None,
    parsed_response_path: str | None,
    provider_reported_cost_usd: float | None = None,
    secrets: list[str] | tuple[str, ...] = (),
    normalization_applied: bool | None = None,
    mirrored_relation_count: int | None = None,
    deduplicated_relation_count: int | None = None,
    normalized_relation_pairs: list[list[str]] | None = None,
    mirrored_relation_pairs: list[list[str]] | None = None,
    raw_validator_passed: bool | None = None,
    raw_validator_errors: list[str] | None = None,
    normalized_response_path: str | None = None,
) -> dict[str, Any]:
    list_usd = estimated_list_price_usd(
        model,
        prompt_tokens,
        completion_tokens,
    )
    payload = {
        "benchmark_schema_version": schema_version,
        "benchmark_input_schema_version": input_schema_version,
        "provider": provider,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "timestamp_utc": timestamp_utc,
        "latency_ms": latency_ms,
        "http_status": http_status,
        "retry_count": retry_count,
        "success": success,
        "failure_reason": failure_reason,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "provider_request_id": provider_request_id,
        "validator_passed": validator_passed,
        "validator_error_count": validator_error_count,
        "validator_errors": validator_errors,
        "selected_event_ids": selected_event_ids,
        "raw_response_path": raw_response_path,
        "parsed_response_path": parsed_response_path,
        "normalized_response_path": normalized_response_path,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "estimated_list_price_usd": list_usd,
        "estimated_list_price_inr": estimated_list_price_inr(list_usd),
        "cost_notes": (
            "provider_reported_cost_usd is null unless the provider returned "
            "an explicit billed amount. estimated_list_price_* are diagnostic "
            "list-price equivalents only, not billed/free-tier cost."
        ),
        "normalization_applied": normalization_applied,
        "mirrored_relation_count": mirrored_relation_count,
        "deduplicated_relation_count": deduplicated_relation_count,
        "normalized_relation_pairs": normalized_relation_pairs,
        "mirrored_relation_pairs": mirrored_relation_pairs,
        "raw_validator_passed": raw_validator_passed,
        "raw_validator_errors": raw_validator_errors,
    }
    return redact_secrets(payload, secrets)
