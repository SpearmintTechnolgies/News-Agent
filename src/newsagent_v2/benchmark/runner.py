"""
Provider-independent editorial benchmark runner.

Wires Groq (or a future provider) to the frozen local validator.
Does not run unless invoked; this module is not hooked into production publish.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.benchmark.contract import (
    EDITORIAL_INPUT_SCHEMA_VERSION,
    EDITORIAL_OUTPUT_SCHEMA_VERSION,
)
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.normalize_relations import (
    normalize_same_event_relations,
)
from newsagent_v2.benchmark.telemetry import build_telemetry, redact_secrets
from newsagent_v2.benchmark.validate import validate_editorial_output
from newsagent_v2.providers.groq_editorial import (
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TIMEOUT_SECONDS,
    GROQ_CHAT_COMPLETIONS_URL,
    GROQ_MODEL_ID,
    GroqUnavailableError,
    build_chat_request_body,
    extract_provider_reported_cost_usd,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    public_request_metadata,
    resolve_groq_api_key,
)

DEFAULT_RUNS_ROOT = (
    Path(__file__).resolve().parents[3] / "output" / "benchmarks" / "runs"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_id(now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def persist_run_artifacts(
    run_dir: Path,
    *,
    request_metadata: dict[str, Any],
    raw_response: Any,
    parsed_output: Any,
    validation: dict[str, Any],
    telemetry: dict[str, Any],
    secrets: list[str] | tuple[str, ...] = (),
    normalized_output: Any = None,
    raw_validation: dict[str, Any] | None = None,
) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "request_metadata": run_dir / "request_metadata.json",
        "raw_provider_response": run_dir / "raw_provider_response.json",
        "parsed_editorial_output": run_dir / "parsed_editorial_output.json",
        "normalized_editorial_output": run_dir / "normalized_editorial_output.json",
        "raw_validation": run_dir / "raw_validation.json",
        "validation": run_dir / "validation.json",
        "telemetry": run_dir / "telemetry.json",
    }
    write_json_utf8(paths["request_metadata"], redact_secrets(request_metadata, secrets))
    write_json_utf8(paths["raw_provider_response"], redact_secrets(raw_response, secrets))
    write_json_utf8(
        paths["parsed_editorial_output"],
        redact_secrets(parsed_output, secrets),
    )
    write_json_utf8(
        paths["normalized_editorial_output"],
        redact_secrets(normalized_output, secrets),
    )
    write_json_utf8(
        paths["raw_validation"],
        redact_secrets(raw_validation, secrets),
    )
    write_json_utf8(paths["validation"], redact_secrets(validation, secrets))
    write_json_utf8(paths["telemetry"], redact_secrets(telemetry, secrets))
    return {name: str(path) for name, path in paths.items()}


def run_groq_editorial_benchmark(
    benchmark_input: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    http_post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    persist_root: Path | None = None,
    persist: bool = False,
    now: datetime | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = perf_counter()
    timestamp = now or _utc_now()
    run_id = _run_id(timestamp)
    secrets: list[str] = []
    api_key = resolve_groq_api_key(environ)
    if api_key:
        secrets.append(api_key)

    body = build_chat_request_body(benchmark_input)
    metadata = public_request_metadata(body, timeout_seconds=timeout_seconds)

    result: dict[str, Any] = {
        "run_id": run_id,
        "provider": "groq",
        "model": GROQ_MODEL_ID,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "endpoint": GROQ_CHAT_COMPLETIONS_URL,
        "request_body": body,
        "request_metadata": metadata,
        "success": False,
        "http_called": False,
        "retry_count": 0,
        "parsed_output": None,
        "validator_errors": None,
        "validator_passed": None,
        "telemetry": None,
        "artifact_paths": None,
    }

    run_dir = None
    if persist:
        root = persist_root or DEFAULT_RUNS_ROOT
        run_dir = root / run_id

    def _finish(
        *,
        success: bool,
        failure_reason: str | None,
        http_status: int | None,
        retry_count: int,
        usage: dict[str, int | None] | None = None,
        provider_request_id: str | None = None,
        provider_reported_cost_usd: float | None = None,
        parsed_output: dict[str, Any] | None = None,
        normalized_output: dict[str, Any] | None = None,
        validator_errors: list[str] | None = None,
        raw_validator_errors: list[str] | None = None,
        normalization_stats: dict[str, Any] | None = None,
        raw_response: Any = None,
        http_called: bool,
    ) -> dict[str, Any]:
        latency_ms = int(round((perf_counter() - started) * 1000))
        usage = usage or {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        validator_passed = None
        error_count = None
        selected_ids = None
        if validator_errors is not None:
            validator_passed = len(validator_errors) == 0
            error_count = len(validator_errors)
        raw_validator_passed = None
        if raw_validator_errors is not None:
            raw_validator_passed = len(raw_validator_errors) == 0
        stats = normalization_stats or {}
        if isinstance(parsed_output, dict):
            ids = parsed_output.get("selected_event_ids")
            if isinstance(ids, list):
                selected_ids = ids

        telemetry = build_telemetry(
            schema_version=EDITORIAL_OUTPUT_SCHEMA_VERSION,
            input_schema_version=str(
                benchmark_input.get(
                    "schema_version",
                    EDITORIAL_INPUT_SCHEMA_VERSION,
                )
            ),
            provider="groq",
            model=GROQ_MODEL_ID,
            reasoning_effort=DEFAULT_REASONING_EFFORT,
            timestamp_utc=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            latency_ms=latency_ms,
            http_status=http_status,
            retry_count=retry_count,
            success=success,
            failure_reason=failure_reason,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            provider_request_id=provider_request_id,
            validator_passed=validator_passed,
            validator_error_count=error_count,
            validator_errors=validator_errors,
            selected_event_ids=selected_ids,
            raw_response_path=None,
            parsed_response_path=None,
            provider_reported_cost_usd=provider_reported_cost_usd,
            secrets=secrets,
            normalization_applied=stats.get("normalization_applied"),
            mirrored_relation_count=stats.get("mirrored_relation_count"),
            deduplicated_relation_count=stats.get("deduplicated_relation_count"),
            normalized_relation_pairs=stats.get("normalized_relation_pairs"),
            mirrored_relation_pairs=stats.get("mirrored_relation_pairs"),
            raw_validator_passed=raw_validator_passed,
            raw_validator_errors=raw_validator_errors,
        )

        validation_doc = {
            "passed": validator_passed,
            "error_count": error_count,
            "errors": validator_errors,
            "stage": "normalized",
        }
        raw_validation_doc = {
            "passed": raw_validator_passed,
            "error_count": (
                None
                if raw_validator_errors is None
                else len(raw_validator_errors)
            ),
            "errors": raw_validator_errors,
            "stage": "raw",
        }

        artifact_paths = None
        if persist and run_dir is not None:
            artifact_paths = persist_run_artifacts(
                run_dir,
                request_metadata=metadata,
                raw_response=raw_response,
                parsed_output=parsed_output,
                normalized_output=normalized_output,
                raw_validation=raw_validation_doc,
                validation=validation_doc,
                telemetry=telemetry,
                secrets=secrets,
            )
            telemetry["raw_response_path"] = artifact_paths["raw_provider_response"]
            telemetry["parsed_response_path"] = artifact_paths[
                "parsed_editorial_output"
            ]
            telemetry["normalized_response_path"] = artifact_paths[
                "normalized_editorial_output"
            ]
            write_json_utf8(
                Path(artifact_paths["telemetry"]),
                redact_secrets(telemetry, secrets),
            )

        result.update(
            {
                "success": success,
                "http_called": http_called,
                "retry_count": retry_count,
                "http_status": http_status,
                "failure_reason": failure_reason,
                "parsed_output": parsed_output,
                "normalized_output": normalized_output,
                "validator_errors": validator_errors,
                "validator_passed": validator_passed,
                "raw_validator_errors": raw_validator_errors,
                "raw_validator_passed": raw_validator_passed,
                "normalization_stats": stats,
                "telemetry": telemetry,
                "artifact_paths": artifact_paths,
                "raw_response": raw_response,
            }
        )
        return result

    if not api_key:
        return _finish(
            success=False,
            failure_reason="provider unavailable: GROQ_API_KEY is not set",
            http_status=None,
            retry_count=0,
            http_called=False,
            raw_response=None,
        )

    try:
        http_result = post_chat_completion(
            body,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            http_post=http_post,
            sleep=sleep,
        )
    except GroqUnavailableError:
        return _finish(
            success=False,
            failure_reason="provider unavailable: GROQ_API_KEY is not set",
            http_status=None,
            retry_count=0,
            http_called=False,
            raw_response=None,
        )

    http_status = http_result.get("status_code")
    retry_count = int(http_result.get("retry_count") or 0)
    raw_payload = http_result.get("payload")
    request_id = http_result.get("provider_request_id")

    if not http_result.get("ok"):
        return _finish(
            success=False,
            failure_reason=str(http_result.get("error") or "HTTP error"),
            http_status=http_status if isinstance(http_status, int) else None,
            retry_count=retry_count,
            http_called=True,
            raw_response=raw_payload,
            provider_request_id=request_id if isinstance(request_id, str) else None,
        )

    if not isinstance(raw_payload, dict):
        return _finish(
            success=False,
            failure_reason="malformed provider response",
            http_status=http_status if isinstance(http_status, int) else None,
            retry_count=retry_count,
            http_called=True,
            raw_response=raw_payload,
            provider_request_id=request_id if isinstance(request_id, str) else None,
        )

    usage = extract_usage(raw_payload)
    billed = extract_provider_reported_cost_usd(raw_payload)

    try:
        parsed = parse_message_content(raw_payload)
    except Exception:
        return _finish(
            success=False,
            failure_reason="failed to parse provider JSON",
            http_status=http_status if isinstance(http_status, int) else None,
            retry_count=retry_count,
            http_called=True,
            raw_response=raw_payload,
            usage=usage,
            provider_request_id=request_id if isinstance(request_id, str) else None,
            provider_reported_cost_usd=billed,
        )

    raw_errors = validate_editorial_output(parsed, benchmark_input)
    normalized, norm_stats = normalize_same_event_relations(parsed)
    errors = validate_editorial_output(normalized, benchmark_input)
    passed = len(errors) == 0
    return _finish(
        success=passed,
        failure_reason=None if passed else "validator rejected editorial output",
        http_status=http_status if isinstance(http_status, int) else None,
        retry_count=retry_count,
        http_called=True,
        raw_response=raw_payload,
        parsed_output=parsed,
        normalized_output=normalized if isinstance(normalized, dict) else None,
        validator_errors=errors,
        raw_validator_errors=raw_errors,
        normalization_stats=norm_stats,
        usage=usage,
        provider_request_id=request_id if isinstance(request_id, str) else None,
        provider_reported_cost_usd=billed,
    )
