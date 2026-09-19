"""Bounded live writer calls for the bake-off. Does not touch /make or images."""

from __future__ import annotations

import json
import re
from time import perf_counter, sleep
from typing import Any, Callable

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GEMINI_36_ARTICLE_FIRST,
    CANDIDATE_GEMINI_38_ARTICLE_FIRST,
    CANDIDATE_GEMINI_ARTICLE_FIRST,
    CANDIDATE_GEMINI_STRUCTURED,
    CANDIDATE_GROQ_ARTICLE_FIRST,
    CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST,
    CANDIDATE_GROQ_QWEN_38_ARTICLE_FIRST,
    GEMINI_38_TEXT_MODEL,
    GEMINI_NEXT_TEXT_MODEL,
    GEMINI_TEXT_MODEL,
    GROQ_LLAMA_33_MODEL,
    GROQ_QWEN_38_MODEL,
    GROQ_MODEL,
    MODE_ARTICLE_FIRST,
    MODE_STRUCTURED,
    PROVIDER_GEMINI,
    PROVIDER_GROQ,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GEMINI_KEY_ENV, GROQ_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.normalize import normalize_article_first, normalize_structured_batch
from newsagent_v2.bench.writer_bakeoff.providers import (
    gemini_article_first_request,
    gemini_text_request,
    groq_article_first_request,
    groq_llama_33_article_first_request,
    groq_qwen_38_article_first_request,
)
from newsagent_v2.article.writer.validate import validate_gemini_generate_content_body
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.telemetry import estimated_list_price_usd, redact_secrets
from newsagent_v2.image.providers.gemini import (
    default_transport as gemini_default_transport,
    estimate_list_price_usd as gemini_estimate_list_price_usd,
    generate_content_url,
    load_gemini_config,
)
from newsagent_v2.providers.groq_article import BATCH_TIMEOUT_SECONDS
from newsagent_v2.providers.groq_editorial import (
    GroqHttpError,
    extract_provider_reported_cost_usd,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    resolve_groq_api_key,
)

GEMINI_TEXT_TIMEOUT_SECONDS = BATCH_TIMEOUT_SECONDS
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _story(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
        "evidence_sufficiency": fixture["article_input"].get("evidence_sufficiency"),
    }


def _secrets(environ: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for key in (GROQ_KEY_ENV, GEMINI_KEY_ENV)
        for value in [str(environ.get(key) or "")]
        if value.strip()
    )


def _parse_json_object(raw: str) -> Any:
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    parsed = json.loads(text)
    return parsed


def _gemini_text(payload: dict[str, Any]) -> str | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return None
    found: list[str] = []
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
            if part.get("thought") is True:
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                found.append(text)
    if not found:
        return None
    return found[-1]


def _gemini_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    usage = payload.get("usageMetadata") or payload.get("usage_metadata")
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def _int(primary: str, secondary: str) -> int | None:
        value = usage.get(primary, usage.get(secondary))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    prompt = _int("promptTokenCount", "prompt_token_count")
    completion = _int("candidatesTokenCount", "candidates_token_count")
    total = _int("totalTokenCount", "total_token_count")
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _gemini_billed(payload: dict[str, Any]) -> float | None:
    for key in ("cost", "total_cost", "cost_usd"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    usage = payload.get("usageMetadata") or payload.get("usage_metadata")
    if isinstance(usage, dict):
        for key in ("cost", "total_cost", "cost_usd"):
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            return float(value)
    return None


def _failed_row(
    *,
    fixture: dict[str, Any],
    candidate_id: str,
    provider: str,
    model: str,
    mode: str,
    http_status: int | None,
    latency_ms: int | None,
    retries: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    estimated_list_price_usd_value: float | None,
    provider_reported_cost_usd: float | None,
    reason: str,
    secrets: tuple[str, ...],
    raw_payload: Any = None,
    native: Any = None,
    parse_ok: bool = False,
) -> dict[str, Any]:
    score = score_result(
        provider=provider,
        model=model,
        mode=mode,
        qa=None,
        article=None,
        article_input=fixture["article_input"],
        http_status=http_status,
        latency_ms=latency_ms,
        retries=retries,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_list_price_usd=estimated_list_price_usd_value,
        provider_reported_cost_usd=provider_reported_cost_usd,
        candidate_id=candidate_id,
        replay=False,
    )
    score["native_parse"] = "PASS" if parse_ok else "FAIL"
    score["normalization"] = "FAIL"
    score["provider_error"] = str(redact_secrets(reason, secrets))
    score["winner_eligible"] = False
    return {
        "score": score,
        "article": None,
        "qa": None,
        "normalized_from": None,
        "generation_failure": str(redact_secrets(reason, secrets)),
        "raw_payload": redact_secrets(raw_payload, secrets) if raw_payload is not None else None,
        "native": redact_secrets(native, secrets) if native is not None else None,
        "live_http_calls": 1,
    }


def _scored_article(
    *,
    fixture: dict[str, Any],
    candidate_id: str,
    provider: str,
    model: str,
    mode: str,
    article: dict[str, Any] | None,
    failure: dict[str, Any] | None,
    normalized_from: str,
    http_status: int | None,
    latency_ms: int | None,
    retries: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    estimated_list_price_usd_value: float | None,
    provider_reported_cost_usd: float | None,
    secrets: tuple[str, ...],
    raw_payload: Any,
    parse_reason: str | None = None,
    native: Any = None,
    parse_ok: bool = True,
    normalize_ok: bool = True,
) -> dict[str, Any]:
    qa = None
    if isinstance(article, dict):
        qa = run_article_qa(article, fixture["article_input"], article_mode="normal")
    elif failure or parse_reason:
        return _failed_row(
            fixture=fixture,
            candidate_id=candidate_id,
            provider=provider,
            model=model,
            mode=mode,
            http_status=http_status,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_list_price_usd_value=estimated_list_price_usd_value,
            provider_reported_cost_usd=provider_reported_cost_usd,
            reason=parse_reason or str((failure or {}).get("reason") or "normalization_failed"),
            secrets=secrets,
            raw_payload=raw_payload,
            native=native,
            parse_ok=parse_ok,
        )
    score = score_result(
        provider=provider,
        model=model,
        mode=mode,
        qa=qa,
        article=article,
        article_input=fixture["article_input"],
        http_status=http_status,
        latency_ms=latency_ms,
        retries=retries,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_list_price_usd=estimated_list_price_usd_value,
        provider_reported_cost_usd=provider_reported_cost_usd,
        candidate_id=candidate_id,
        replay=False,
    )
    score["native_parse"] = "PASS" if parse_ok else "FAIL"
    score["normalization"] = "PASS" if normalize_ok else "FAIL"
    score["provider_error"] = None
    if score["normalization"] != "PASS" or score["native_parse"] != "PASS":
        score["winner_eligible"] = False
    return {
        "score": score,
        "article": article,
        "qa": qa,
        "normalized_from": normalized_from,
        "generation_failure": None,
        "raw_payload": redact_secrets(raw_payload, secrets),
        "native": redact_secrets(native, secrets) if native is not None else None,
        "live_http_calls": 1,
    }


def execute_groq_article_first(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    http_post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    secrets = _secrets(environ)
    api_key = resolve_groq_api_key(environ)
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    body = groq_article_first_request(fixture)
    started = perf_counter()
    result = post_chat_completion(
        body,
        api_key=api_key,
        timeout_seconds=BATCH_TIMEOUT_SECONDS,
        http_post=http_post,
        sleep=sleep,
    )
    latency_ms = int((perf_counter() - started) * 1000)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    usage = extract_usage(payload) if payload else {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    billed = extract_provider_reported_cost_usd(payload) if payload else None
    estimate = estimated_list_price_usd(
        GROQ_MODEL,
        usage["prompt_tokens"],
        usage["completion_tokens"],
    )
    retries = int(result.get("retry_count") or 0)
    status = result.get("status_code")
    if not result.get("ok") or status != 200 or payload is None:
        return _failed_row(
            fixture=fixture,
            candidate_id=CANDIDATE_GROQ_ARTICLE_FIRST,
            provider=PROVIDER_GROQ,
            model=GROQ_MODEL,
            mode=MODE_ARTICLE_FIRST,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=estimate,
            provider_reported_cost_usd=billed,
            reason=str(result.get("error") or f"HTTP {status}"),
            secrets=secrets,
            raw_payload=payload,
        )
    try:
        parsed = parse_message_content(payload)
    except GroqHttpError as exc:
        return _failed_row(
            fixture=fixture,
            candidate_id=CANDIDATE_GROQ_ARTICLE_FIRST,
            provider=PROVIDER_GROQ,
            model=GROQ_MODEL,
            mode=MODE_ARTICLE_FIRST,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=estimate,
            provider_reported_cost_usd=billed,
            reason=str(exc),
            secrets=secrets,
            raw_payload=payload,
        )
    normalized = normalize_article_first(
        parsed,
        event_id=fixture["manifest"]["event_id"],
        story=_story(fixture),
    )
    return _scored_article(
        fixture=fixture,
        candidate_id=CANDIDATE_GROQ_ARTICLE_FIRST,
        provider=PROVIDER_GROQ,
        model=GROQ_MODEL,
        mode=MODE_ARTICLE_FIRST,
        article=normalized.get("article") if isinstance(normalized.get("article"), dict) else None,
        failure=normalized.get("failure") if isinstance(normalized.get("failure"), dict) else None,
        normalized_from="article_first",
        http_status=status if isinstance(status, int) else None,
        latency_ms=latency_ms,
        retries=retries,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        estimated_list_price_usd_value=estimate,
        provider_reported_cost_usd=billed,
        secrets=secrets,
        raw_payload=payload,
        native=parsed,
        parse_ok=True,
        normalize_ok=bool(normalized.get("ok") and isinstance(normalized.get("article"), dict)),
    )


def execute_groq_llama_33_article_first(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    http_post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    secrets = _secrets(environ)
    api_key = resolve_groq_api_key(environ)
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    body = groq_llama_33_article_first_request(fixture)
    started = perf_counter()
    result = post_chat_completion(
        body,
        api_key=api_key,
        timeout_seconds=BATCH_TIMEOUT_SECONDS,
        http_post=http_post,
        sleep=sleep,
        max_attempts=1,
    )
    latency_ms = int((perf_counter() - started) * 1000)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    usage = extract_usage(payload) if payload else {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    billed = extract_provider_reported_cost_usd(payload) if payload else None
    retries = int(result.get("retry_count") or 0)
    status = result.get("status_code")
    if not result.get("ok") or status != 200 or payload is None:
        return _failed_row(
            fixture=fixture,
            candidate_id=CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST,
            provider=PROVIDER_GROQ,
            model=GROQ_LLAMA_33_MODEL,
            mode=MODE_ARTICLE_FIRST,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=None,
            provider_reported_cost_usd=billed,
            reason=str(result.get("error") or f"HTTP {status}"),
            secrets=secrets,
            raw_payload=payload,
        )
    try:
        parsed = parse_message_content(payload)
    except GroqHttpError as exc:
        return _failed_row(
            fixture=fixture,
            candidate_id=CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST,
            provider=PROVIDER_GROQ,
            model=GROQ_LLAMA_33_MODEL,
            mode=MODE_ARTICLE_FIRST,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=None,
            provider_reported_cost_usd=billed,
            reason=str(exc),
            secrets=secrets,
            raw_payload=payload,
        )
    normalized = normalize_article_first(
        parsed,
        event_id=fixture["manifest"]["event_id"],
        story=_story(fixture),
    )
    return _scored_article(
        fixture=fixture,
        candidate_id=CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST,
        provider=PROVIDER_GROQ,
        model=GROQ_LLAMA_33_MODEL,
        mode=MODE_ARTICLE_FIRST,
        article=normalized.get("article") if isinstance(normalized.get("article"), dict) else None,
        failure=normalized.get("failure") if isinstance(normalized.get("failure"), dict) else None,
        normalized_from="article_first",
        http_status=status if isinstance(status, int) else None,
        latency_ms=latency_ms,
        retries=retries,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        estimated_list_price_usd_value=None,
        provider_reported_cost_usd=billed,
        secrets=secrets,
        raw_payload=payload,
        native=parsed,
        parse_ok=True,
        normalize_ok=bool(normalized.get("ok") and isinstance(normalized.get("article"), dict)),
    )


def execute_groq_qwen_38_article_first(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    http_post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    secrets = _secrets(environ)
    api_key = resolve_groq_api_key(environ)
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    body = groq_qwen_38_article_first_request(fixture)
    started = perf_counter()
    result = post_chat_completion(
        body,
        api_key=api_key,
        timeout_seconds=BATCH_TIMEOUT_SECONDS,
        http_post=http_post,
        sleep=sleep,
        max_attempts=1,
    )
    latency_ms = int((perf_counter() - started) * 1000)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    usage = extract_usage(payload) if payload else {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    billed = extract_provider_reported_cost_usd(payload) if payload else None
    retries = int(result.get("retry_count") or 0)
    status = result.get("status_code")
    if not result.get("ok") or status != 200 or payload is None:
        return _failed_row(
            fixture=fixture,
            candidate_id=CANDIDATE_GROQ_QWEN_38_ARTICLE_FIRST,
            provider=PROVIDER_GROQ,
            model=GROQ_QWEN_38_MODEL,
            mode=MODE_ARTICLE_FIRST,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=None,
            provider_reported_cost_usd=billed,
            reason=str(result.get("error") or f"HTTP {status}"),
            secrets=secrets,
            raw_payload=payload,
        )
    try:
        parsed = parse_message_content(payload)
    except GroqHttpError as exc:
        return _failed_row(
            fixture=fixture,
            candidate_id=CANDIDATE_GROQ_QWEN_38_ARTICLE_FIRST,
            provider=PROVIDER_GROQ,
            model=GROQ_QWEN_38_MODEL,
            mode=MODE_ARTICLE_FIRST,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=retries,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=None,
            provider_reported_cost_usd=billed,
            reason=str(exc),
            secrets=secrets,
            raw_payload=payload,
        )
    normalized = normalize_article_first(
        parsed,
        event_id=fixture["manifest"]["event_id"],
        story=_story(fixture),
    )
    return _scored_article(
        fixture=fixture,
        candidate_id=CANDIDATE_GROQ_QWEN_38_ARTICLE_FIRST,
        provider=PROVIDER_GROQ,
        model=GROQ_QWEN_38_MODEL,
        mode=MODE_ARTICLE_FIRST,
        article=normalized.get("article") if isinstance(normalized.get("article"), dict) else None,
        failure=normalized.get("failure") if isinstance(normalized.get("failure"), dict) else None,
        normalized_from="article_first",
        http_status=status if isinstance(status, int) else None,
        latency_ms=latency_ms,
        retries=retries,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        estimated_list_price_usd_value=None,
        provider_reported_cost_usd=billed,
        secrets=secrets,
        raw_payload=payload,
        native=parsed,
        parse_ok=True,
        normalize_ok=bool(normalized.get("ok") and isinstance(normalized.get("article"), dict)),
    )


def execute_gemini(
    fixture: dict[str, Any],
    *,
    mode: str,
    candidate_id: str,
    environ: dict[str, str],
    transport: Callable[..., Any] | None = None,
    model: str = GEMINI_TEXT_MODEL,
) -> dict[str, Any]:
    secrets = _secrets(environ)
    config = load_gemini_config(environ)
    body = gemini_article_first_request(fixture) if mode != MODE_STRUCTURED else gemini_text_request(fixture, mode=mode)
    schema_check = validate_gemini_generate_content_body(body)
    if not schema_check.get("ok") or schema_check.get("has_additionalProperties"):
        return _failed_row(
            fixture=fixture,
            candidate_id=candidate_id,
            provider=PROVIDER_GEMINI,
            model=model,
            mode=mode,
            http_status=None,
            latency_ms=0,
            retries=0,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            estimated_list_price_usd_value=None,
            provider_reported_cost_usd=None,
            reason="gemini_schema_validation_failed: " + ",".join(schema_check.get("violations") or ["additionalProperties"]),
            secrets=secrets,
        )
    url = generate_content_url(model)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": config.api_key,
    }
    poster = transport or gemini_default_transport
    started = perf_counter()
    response = poster(
        url,
        headers=headers,
        json_body=body,
        timeout=GEMINI_TEXT_TIMEOUT_SECONDS,
    )
    latency_ms = int((perf_counter() - started) * 1000)
    status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        payload = None
    usage = _gemini_usage(payload) if payload else {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    billed = _gemini_billed(payload) if payload else None
    estimate = gemini_estimate_list_price_usd(
        prompt_tokens=usage["prompt_tokens"],
        image_output_tokens=None,
        text_output_tokens=usage["completion_tokens"],
    )
    if status != 200 or payload is None:
        reason = f"HTTP {status}"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                reason = str(error["message"])[:500]
        return _failed_row(
            fixture=fixture,
            candidate_id=candidate_id,
            provider=PROVIDER_GEMINI,
            model=model,
            mode=mode,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=0,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=estimate,
            provider_reported_cost_usd=billed,
            reason=reason,
            secrets=secrets,
            raw_payload=payload,
        )
    text = _gemini_text(payload)
    if not text:
        return _failed_row(
            fixture=fixture,
            candidate_id=candidate_id,
            provider=PROVIDER_GEMINI,
            model=model,
            mode=mode,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=0,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=estimate,
            provider_reported_cost_usd=billed,
            reason="missing_text",
            secrets=secrets,
            raw_payload=payload,
        )
    try:
        parsed = _parse_json_object(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return _failed_row(
            fixture=fixture,
            candidate_id=candidate_id,
            provider=PROVIDER_GEMINI,
            model=model,
            mode=mode,
            http_status=status if isinstance(status, int) else None,
            latency_ms=latency_ms,
            retries=0,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            estimated_list_price_usd_value=estimate,
            provider_reported_cost_usd=billed,
            reason=f"provider content was not valid JSON: {exc}",
            secrets=secrets,
            raw_payload=payload,
        )
    event_id = fixture["manifest"]["event_id"]
    if mode == MODE_STRUCTURED:
        normalized = normalize_structured_batch(
            parsed,
            event_id=event_id,
            story=_story(fixture),
        )
        normalized_from = "structured_batch"
    else:
        normalized = normalize_article_first(
            parsed,
            event_id=event_id,
            story=_story(fixture),
        )
        normalized_from = "article_first"
    return _scored_article(
        fixture=fixture,
        candidate_id=candidate_id,
        provider=PROVIDER_GEMINI,
        model=model,
        mode=mode,
        article=normalized.get("article") if isinstance(normalized.get("article"), dict) else None,
        failure=normalized.get("failure") if isinstance(normalized.get("failure"), dict) else None,
        normalized_from=normalized_from,
        http_status=status if isinstance(status, int) else None,
        latency_ms=latency_ms,
        retries=0,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        estimated_list_price_usd_value=estimate,
        provider_reported_cost_usd=billed,
        secrets=secrets,
        raw_payload=payload,
        native=parsed,
        parse_ok=True,
        normalize_ok=bool(normalized.get("ok") and isinstance(normalized.get("article"), dict)),
    )


def execute_gemini_structured(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    return execute_gemini(
        fixture,
        mode=MODE_STRUCTURED,
        candidate_id=CANDIDATE_GEMINI_STRUCTURED,
        environ=environ,
        transport=transport,
    )


def execute_gemini_article_first(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    return execute_gemini(
        fixture,
        mode=MODE_ARTICLE_FIRST,
        candidate_id=CANDIDATE_GEMINI_ARTICLE_FIRST,
        environ=environ,
        transport=transport,
        model=GEMINI_TEXT_MODEL,
    )


def execute_gemini_36_article_first(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    return execute_gemini(
        fixture,
        mode=MODE_ARTICLE_FIRST,
        candidate_id=CANDIDATE_GEMINI_36_ARTICLE_FIRST,
        environ=environ,
        transport=transport,
        model=GEMINI_NEXT_TEXT_MODEL,
    )


def execute_gemini_38_article_first(
    fixture: dict[str, Any],
    *,
    environ: dict[str, str],
    transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    return execute_gemini(
        fixture,
        mode=MODE_ARTICLE_FIRST,
        candidate_id=CANDIDATE_GEMINI_38_ARTICLE_FIRST,
        environ=environ,
        transport=transport,
        model=GEMINI_38_TEXT_MODEL,
    )
