"""
One-pass Groq article-generation runner.

Offline input + structured Chat Completions. Does not browse, fetch
sources, refresh feeds, publish, or send Telegram. Not hooked into
production.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.article.contract import (
    ARTICLE_INPUT_SCHEMA_VERSION,
    ARTICLE_OUTPUT_SCHEMA_VERSION,
    stamp_article_schema_version,
)
from newsagent_v2.article.expand import expand_provider_article
from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import build_telemetry, redact_secrets
from newsagent_v2.providers.groq_article import (
    DEFAULT_ARTICLE_TIMEOUT_SECONDS,
    GroqSchemaCompatibilityError,
    build_article_chat_request_body,
)
from newsagent_v2.providers.groq_editorial import (
    DEFAULT_REASONING_EFFORT,
    GROQ_CHAT_COMPLETIONS_URL,
    GROQ_MODEL_ID,
    GroqUnavailableError,
    extract_provider_reported_cost_usd,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    public_request_metadata,
    resolve_groq_api_key,
)

DEFAULT_RUNS_ROOT = (
    Path(__file__).resolve().parents[3]
    / "output"
    / "benchmarks"
    / "article_runs"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_id(now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def persist_article_run_artifacts(
    run_dir: Path,
    *,
    generation_input: dict[str, Any],
    request_metadata: dict[str, Any],
    raw_response: Any,
    parsed_output: Any,
    qa_result: Any,
    telemetry: dict[str, Any],
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "generation_input": run_dir / "generation_input.json",
        "request_metadata": run_dir / "request_metadata.json",
        "raw_provider_response": run_dir / "raw_provider_response.json",
        "parsed_article_output": run_dir / "parsed_article_output.json",
        "qa_result": run_dir / "qa_result.json",
        "telemetry": run_dir / "telemetry.json",
    }
    write_json_utf8(paths["generation_input"], redact_secrets(generation_input, secrets))
    write_json_utf8(paths["request_metadata"], redact_secrets(request_metadata, secrets))
    write_json_utf8(paths["raw_provider_response"], redact_secrets(raw_response, secrets))
    write_json_utf8(paths["parsed_article_output"], redact_secrets(parsed_output, secrets))
    write_json_utf8(paths["qa_result"], redact_secrets(qa_result, secrets))
    write_json_utf8(paths["telemetry"], redact_secrets(telemetry, secrets))
    return {name: str(path) for name, path in paths.items()}


def run_groq_article_generation(
    article_input: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
    http_post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    persist_root: Path | None = None,
    persist: bool = False,
    now: datetime | None = None,
    timeout_seconds: int = DEFAULT_ARTICLE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = perf_counter()
    timestamp = now or _utc_now()
    run_id = _run_id(timestamp)
    secrets: list[str] = []
    api_key = resolve_groq_api_key(environ)
    if api_key:
        secrets.append(api_key)

    event_id = article_input.get("event_id") if isinstance(article_input, dict) else None

    result: dict[str, Any] = {
        "run_id": run_id,
        "provider": "groq",
        "model": GROQ_MODEL_ID,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "endpoint": GROQ_CHAT_COMPLETIONS_URL,
        "event_id": event_id,
        "success": False,
        "http_called": False,
        "retry_count": 0,
        "http_request_count": 0,
        "parsed_output": None,
        "qa_result": None,
        "telemetry": None,
        "artifact_paths": None,
    }

    run_dir = None
    if persist:
        root = persist_root or DEFAULT_RUNS_ROOT
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json_utf8(
            run_dir / "generation_input.json",
            redact_secrets(article_input, secrets),
        )

    body: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    try:
        body = build_article_chat_request_body(article_input)
        metadata = public_request_metadata(body, timeout_seconds=timeout_seconds)
    except GroqSchemaCompatibilityError as exc:
        metadata = {
            "method": "POST",
            "url": GROQ_CHAT_COMPLETIONS_URL,
            "timeout_seconds": timeout_seconds,
            "body": None,
            "tools_enabled": False,
            "headers_included": False,
            "schema_compatibility_error": str(exc),
        }

    result["request_body"] = body
    result["request_metadata"] = metadata

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
        qa_result: dict[str, Any] | None = None,
        raw_response: Any = None,
        http_called: bool,
    ) -> dict[str, Any]:
        latency_ms = int(round((perf_counter() - started) * 1000))
        usage = usage or {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        qa_passed = None
        qa_error_count = None
        qa_errors: list[str] | None = None
        if isinstance(qa_result, dict):
            qa_passed = bool(qa_result.get("qa_passed"))
            critical = qa_result.get("critical_failures") or []
            qa_error_count = len(critical) if isinstance(critical, list) else None
            if isinstance(critical, list):
                qa_errors = [
                    f"{item.get('code')}: {item.get('message')}"
                    for item in critical
                    if isinstance(item, dict)
                ]

        http_request_count = 0
        if http_called:
            http_request_count = retry_count + 1

        telemetry = build_telemetry(
            schema_version=ARTICLE_OUTPUT_SCHEMA_VERSION,
            input_schema_version=str(
                article_input.get(
                    "schema_version",
                    ARTICLE_INPUT_SCHEMA_VERSION,
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
            validator_passed=qa_passed,
            validator_error_count=qa_error_count,
            validator_errors=qa_errors,
            selected_event_ids=[event_id] if isinstance(event_id, str) else None,
            raw_response_path=None,
            parsed_response_path=None,
            provider_reported_cost_usd=provider_reported_cost_usd,
            secrets=secrets,
        )
        telemetry["task"] = "article-generation"
        telemetry["event_id"] = event_id
        telemetry["http_called"] = http_called
        telemetry["http_request_count"] = http_request_count
        telemetry["qa_passed"] = qa_passed
        telemetry["publishable"] = (
            qa_result.get("publishable") if isinstance(qa_result, dict) else None
        )

        artifact_paths = None
        if persist and run_dir is not None:
            artifact_paths = persist_article_run_artifacts(
                run_dir,
                generation_input=article_input,
                request_metadata=metadata or {},
                raw_response=raw_response,
                parsed_output=parsed_output,
                qa_result=qa_result,
                telemetry=telemetry,
                secrets=secrets,
            )
            telemetry["raw_response_path"] = artifact_paths["raw_provider_response"]
            telemetry["parsed_response_path"] = artifact_paths["parsed_article_output"]
            write_json_utf8(
                Path(artifact_paths["telemetry"]),
                redact_secrets(telemetry, secrets),
            )

        result.update(
            {
                "success": success,
                "http_called": http_called,
                "retry_count": retry_count,
                "http_request_count": http_request_count,
                "http_status": http_status,
                "failure_reason": failure_reason,
                "parsed_output": parsed_output,
                "qa_result": qa_result,
                "telemetry": telemetry,
                "artifact_paths": artifact_paths,
                "raw_response": raw_response,
            }
        )
        return result

    if body is None:
        return _finish(
            success=False,
            failure_reason="provider schema compatibility check failed",
            http_status=None,
            retry_count=0,
            http_called=False,
            raw_response=None,
        )

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
        if isinstance(parsed, dict):
            stamp_article_schema_version(parsed)
            expand_provider_article(parsed, article_input)
            materialize_article(parsed)
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

    qa_result = run_article_qa(parsed, article_input)
    qa_passed = bool(qa_result.get("qa_passed"))
    return _finish(
        success=qa_passed,
        failure_reason=None if qa_passed else "article QA rejected output",
        http_status=http_status if isinstance(http_status, int) else None,
        retry_count=retry_count,
        http_called=True,
        raw_response=raw_payload,
        parsed_output=parsed,
        qa_result=qa_result,
        usage=usage,
        provider_request_id=request_id if isinstance(request_id, str) else None,
        provider_reported_cost_usd=billed,
    )
