"""Single Groq Chat Completions call for all sufficient Top-5 stories."""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from newsagent_v2.article.contract import REQUIRED_ARTICLE_FIELDS, stamp_article_schema_version
from newsagent_v2.article.enrich import STATUS_INSUFFICIENT
from newsagent_v2.article.expand import expand_provider_article
from newsagent_v2.article.render import materialize_article
from newsagent_v2.batch.completeness import account_requested_ids
from newsagent_v2.benchmark.telemetry import estimated_list_price_usd, redact_secrets
from newsagent_v2.article.prompt_evidence import compact_story_evidence
from newsagent_v2.providers.groq_article import (
    BATCH_TIMEOUT_SECONDS,
    GroqSchemaCompatibilityError,
    build_top5_article_batch_request_body,
)
from newsagent_v2.providers.groq_editorial import (
    GROQ_MODEL_ID,
    GroqUnavailableError,
    extract_provider_reported_cost_usd,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    resolve_groq_api_key,
    safe_chat_request_diagnostics,
    sanitize_provider_error,
)

ARTICLE_COMPLETE_FIELDS = REQUIRED_ARTICLE_FIELDS + ("generation_notes",)


def _article_complete(article: Any) -> bool:
    if not isinstance(article, dict):
        return False
    for field in ARTICLE_COMPLETE_FIELDS:
        if field not in article:
            return False
    return bool(
        article.get("event_id")
        and (
            article.get("article_body")
            or article.get("headline")
            or article.get("article_sections")
        )
    )


def normalize_batch_output(
    parsed: Any,
    *,
    requested_ids: list[str],
    stories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    articles_by_id: dict[str, dict[str, Any]] = {}
    failures_by_id: dict[str, dict[str, str]] = {}
    if not isinstance(parsed, dict):
        for event_id in requested_ids:
            failures_by_id[event_id] = {
                "event_id": event_id,
                "code": "malformed_batch_output",
                "reason": "provider JSON was not a batch object",
            }
        return {
            "articles_by_id": {},
            "failures_by_id": failures_by_id,
            "completeness": account_requested_ids(requested_ids, {}, failures_by_id),
        }

    story_by_id = {
        str(row.get("event_id") or ""): row
        for row in (stories or [])
        if isinstance(row, dict) and row.get("event_id")
    }

    for item in parsed.get("articles") or []:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("event_id") or "")
        if not event_id:
            continue
        row = story_by_id.get(event_id) or {}
        pack = row.get("article_input") if isinstance(row.get("article_input"), dict) else {"event_id": event_id}
        stamp_article_schema_version(item)
        expand_provider_article(item, pack)
        materialize_article(item)
        if not _article_complete(item):
            failures_by_id[event_id] = {
                "event_id": event_id,
                "code": "malformed_article_object",
                "reason": "article object missing required article-output-v1 fields",
            }
            continue
        if event_id in articles_by_id:
            failures_by_id[event_id] = {
                "event_id": event_id,
                "code": "duplicate_event_ids",
                "reason": "duplicate article event_id in provider response",
            }
            articles_by_id.pop(event_id, None)
            continue
        articles_by_id[event_id] = item

    for item in parsed.get("failures") or []:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("event_id") or "")
        if not event_id:
            continue
        failures_by_id[event_id] = {
            "event_id": event_id,
            "code": str(item.get("code") or "article_generation_failed"),
            "reason": str(item.get("reason") or "provider reported failure")[:300],
        }

    overlap = set(articles_by_id) & set(failures_by_id)
    for event_id in overlap:
        articles_by_id.pop(event_id, None)
        failures_by_id[event_id] = {
            "event_id": event_id,
            "code": "event_id_overlap",
            "reason": "event_id appeared in both articles and failures",
        }

    unknown = (set(articles_by_id) | set(failures_by_id)) - set(requested_ids)
    for event_id in unknown:
        articles_by_id.pop(event_id, None)
        failures_by_id.pop(event_id, None)

    report = account_requested_ids(requested_ids, articles_by_id, failures_by_id)
    for event_id in report["missing_ids"]:
        failures_by_id[event_id] = {
            "event_id": event_id,
            "code": "unaccounted_event",
            "reason": "requested event_id was omitted from the provider response",
        }
    report = account_requested_ids(requested_ids, articles_by_id, failures_by_id)
    return {
        "articles_by_id": articles_by_id,
        "failures_by_id": failures_by_id,
        "completeness": report,
    }


def _empty_telemetry(*, batch_id: str, requested_ids: list[str], http_request_count: int) -> dict[str, Any]:
    return {
        "editorial_ai_request_count": http_request_count,
        "provider": "groq",
        "model": GROQ_MODEL_ID,
        "request_count": http_request_count,
        "latency_ms": 0,
        "http_status": None,
        "retries": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "provider_reported_cost_usd": None,
        "estimated_list_price_usd": None,
        "http_called": False,
        "batch_id": batch_id,
        "requested_event_ids": list(requested_ids),
        "provider_error": None,
        "request_diagnostics": {},
        "retry_after": None,
        "retry_slept_seconds": 0,
        "attempts": 0,
    }


def generate_make_articles(
    rows: list[dict[str, Any]],
    *,
    environ: dict[str, str] | None = None,
    http_post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = batch_id or f"{stamp}-{uuid4().hex[:8]}"
    all_ids = [str(row["event_id"]) for row in rows]
    failures: dict[str, dict[str, str]] = {}
    sufficient: list[dict[str, Any]] = []
    for row in rows:
        event_id = str(row["event_id"])
        if (
            row.get("skip_reason") == STATUS_INSUFFICIENT
            or (row.get("evidence_sufficiency") or {}).get("status") == STATUS_INSUFFICIENT
        ):
            row["skip_reason"] = STATUS_INSUFFICIENT
            failures[event_id] = {
                "event_id": event_id,
                "code": STATUS_INSUFFICIENT,
                "reason": "evidence cannot support a 350-word factual article without filler",
            }
        else:
            compact = compact_story_evidence(row)
            row["prompt_compaction"] = compact.get("prompt_compaction")
            sufficient.append(row)

    if not sufficient:
        telemetry = _empty_telemetry(batch_id=run_id, requested_ids=all_ids, http_request_count=0)
        return {
            "articles": {},
            "failures": failures,
            "telemetry": telemetry,
            "http_request_count": 0,
            "completeness": account_requested_ids(all_ids, {}, failures),
        }

    started = perf_counter()
    requested = [str(row["event_id"]) for row in sufficient]
    secrets: list[str] = []
    api_key = resolve_groq_api_key(environ)
    if api_key:
        secrets.append(api_key)
    try:
        body = build_top5_article_batch_request_body(batch_id=run_id, stories=sufficient)
    except GroqSchemaCompatibilityError:
        for event_id in requested:
            failures[event_id] = {
                "event_id": event_id,
                "code": "schema_incompatible",
                "reason": "provider schema compatibility check failed",
            }
        telemetry = _empty_telemetry(batch_id=run_id, requested_ids=all_ids, http_request_count=0)
        return {
            "articles": {},
            "failures": failures,
            "telemetry": telemetry,
            "http_request_count": 0,
            "completeness": account_requested_ids(all_ids, {}, failures),
        }

    if not api_key:
        for event_id in requested:
            failures[event_id] = {
                "event_id": event_id,
                "code": "provider_unavailable",
                "reason": "GROQ_API_KEY is not set",
            }
        telemetry = _empty_telemetry(batch_id=run_id, requested_ids=all_ids, http_request_count=0)
        return {
            "articles": {},
            "failures": failures,
            "telemetry": redact_secrets(telemetry, secrets),
            "http_request_count": 0,
            "completeness": account_requested_ids(all_ids, {}, failures),
        }

    diagnostics = safe_chat_request_diagnostics(body)
    try:
        http_result = post_chat_completion(
            body,
            api_key=api_key,
            timeout_seconds=BATCH_TIMEOUT_SECONDS,
            http_post=http_post,
            sleep=sleep,
        )
    except GroqUnavailableError:
        for event_id in requested:
            failures[event_id] = {
                "event_id": event_id,
                "code": "provider_unavailable",
                "reason": "GROQ_API_KEY is not set",
            }
        telemetry = _empty_telemetry(batch_id=run_id, requested_ids=all_ids, http_request_count=0)
        telemetry["request_diagnostics"] = diagnostics
        return {
            "articles": {},
            "failures": failures,
            "telemetry": redact_secrets(telemetry, secrets),
            "http_request_count": 0,
            "completeness": account_requested_ids(all_ids, {}, failures),
        }

    latency_ms = int((perf_counter() - started) * 1000)
    retry_count = int(http_result.get("retry_count") or 0)
    http_status = http_result.get("status_code")
    raw_payload = http_result.get("payload")
    usage = (
        extract_usage(raw_payload)
        if isinstance(raw_payload, dict)
        else {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    )
    billed = extract_provider_reported_cost_usd(raw_payload) if isinstance(raw_payload, dict) else None
    parsed = None
    provider_error = None
    stamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if http_result.get("ok") and isinstance(raw_payload, dict):
        try:
            parsed = parse_message_content(raw_payload)
        except Exception:
            parsed = None
            for event_id in requested:
                failures[event_id] = {
                    "event_id": event_id,
                    "code": "malformed_provider_json",
                    "reason": "failed to parse provider JSON",
                }
    elif not http_result.get("ok"):
        provider_error = sanitize_provider_error(
            status_code=http_status if isinstance(http_status, int) else None,
            payload=raw_payload,
            headers=http_result.get("headers"),
            secrets=secrets,
            timestamp_utc=stamp_utc,
            model=GROQ_MODEL_ID,
            provider_request_id=http_result.get("provider_request_id"),
            request_diagnostics=diagnostics,
        )
        reason = str(
            (provider_error or {}).get("provider_error_message")
            or http_result.get("error")
            or f"HTTP {http_status}"
        )[:300]
        for event_id in requested:
            failures[event_id] = {
                "event_id": event_id,
                "code": "provider_http_error",
                "reason": reason,
            }

    if parsed is not None:
        normalized = normalize_batch_output(
            parsed,
            requested_ids=requested,
            stories=sufficient,
        )
        articles = normalized.get("articles_by_id") or {}
        failures.update(normalized.get("failures_by_id") or {})
    else:
        articles = {}

    for row in rows:
        event_id = str(row["event_id"])
        if event_id in articles:
            row["article"] = articles[event_id]
        elif event_id in failures and row.get("skip_reason") != STATUS_INSUFFICIENT:
            row["skip_reason"] = failures[event_id].get("code") or "article_generation_failed"
            row["generation_failure"] = failures[event_id].get("reason")

    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    telemetry = {
        "editorial_ai_request_count": 1,
        "provider": "groq",
        "model": GROQ_MODEL_ID,
        "request_count": 1,
        "latency_ms": latency_ms,
        "http_status": http_status if isinstance(http_status, int) else None,
        "retries": retry_count,
        "retry_after": http_result.get("retry_after"),
        "retry_slept_seconds": http_result.get("retry_slept_seconds"),
        "attempts": http_result.get("attempts"),
        "prompt_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
        "completion_tokens": completion_tokens if isinstance(completion_tokens, int) else None,
        "total_tokens": usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else None,
        "provider_reported_cost_usd": billed,
        "estimated_list_price_usd": estimated_list_price_usd(
            GROQ_MODEL_ID,
            prompt_tokens if isinstance(prompt_tokens, int) else None,
            completion_tokens if isinstance(completion_tokens, int) else None,
        ),
        "http_called": True,
        "batch_id": run_id,
        "requested_event_ids": requested,
        "request_diagnostics": diagnostics,
        "provider_error": provider_error,
    }
    all_failures = {eid: item.get("reason") or item.get("code") or "failed" for eid, item in failures.items()}
    return {
        "articles": articles,
        "failures": failures,
        "telemetry": redact_secrets(telemetry, secrets),
        "http_request_count": 1,
        "completeness": account_requested_ids(all_ids, articles, all_failures),
    }
