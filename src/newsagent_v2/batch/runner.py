"""
Top-5 batch orchestrator.

Reuses ranking cut, one optional batched editorial hook, independent
article QA, independent image jobs, and TEST Telegram. Default: no live
LLM, image, or Telegram calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.batch.completeness import account_requested_ids
from newsagent_v2.batch.contract import BATCH_SCHEMA_VERSION, TOP5_COUNT, BatchError
from newsagent_v2.batch.images import run_image_jobs
from newsagent_v2.batch.select import select_top5_event_ids
from newsagent_v2.batch.telemetry import build_batch_telemetry
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import estimated_list_price_usd, redact_secrets
from newsagent_v2.telegram.batch import deliver_top5_batch
from newsagent_v2.telegram.config import TelegramConfig

DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[3] / "output" / "batch_runs"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _qa_reason(qa_result: dict[str, Any] | None) -> str:
    if not isinstance(qa_result, dict):
        return "qa_missing"
    critical = qa_result.get("critical_failures") or []
    if isinstance(critical, list) and critical:
        first = critical[0]
        if isinstance(first, dict):
            code = first.get("code") or "qa_failed"
            message = first.get("message") or ""
            return f"{code}: {message}".strip()
    return "qa_not_publishable"


def run_top5_batch(
    *,
    event_ids: list[str] | None = None,
    ranked_clusters: Any = None,
    evidence_pack: dict[str, Any] | None = None,
    stories: list[dict[str, Any]],
    editorial_fn: Callable[[list[str]], dict[str, Any]] | None = None,
    article_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    qa_fn: Callable[..., dict[str, Any]] | None = None,
    image_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    telegram_config: TelegramConfig | None = None,
    telegram_transport: Callable[..., Any] | None = None,
    live_llm: bool = False,
    live_image: bool = False,
    live_telegram: bool = False,
    parallel_images: bool = False,
    persist: bool = False,
    persist_root: Path | None = None,
    now: datetime | None = None,
    repo_root: Path | None = None,
    provider_telemetry: dict[str, Any] | None = None,
    pipeline_started_at: float | None = None,
    stage_timings: dict[str, Any] | None = None,
    viability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    timestamp = now or _utc_now()
    batch_run_id = _run_id(timestamp)
    selected = select_top5_event_ids(
        ranked_clusters=ranked_clusters,
        evidence_pack=evidence_pack,
        event_ids=event_ids,
    )
    by_id = {str(row.get("event_id")): dict(row) for row in stories}
    if set(by_id) != set(selected):
        raise BatchError(
            "story_id_mismatch",
            "story payloads must match the selected event IDs",
        )
    ordered = [by_id[event_id] for event_id in selected]

    if live_telegram and telegram_config is None:
        raise BatchError("telegram_config_missing", "live Telegram TEST requires TelegramConfig")
    if telegram_config is not None and not telegram_config.test_mode:
        raise BatchError("production_telegram_forbidden", "production Telegram is impossible in TEST batch")

    qa = qa_fn or run_article_qa
    editorial_output = None
    if editorial_fn is not None:
        editorial_output = editorial_fn(selected)
    editorial_tel = dict(provider_telemetry or {})
    editorial_ai_requests = int(editorial_tel.get("editorial_ai_request_count") or 0)
    prompt_tokens = int(editorial_tel.get("prompt_tokens") or 0)
    completion_tokens = int(editorial_tel.get("completion_tokens") or 0)
    total_tokens = int(editorial_tel.get("total_tokens") or 0)
    token_seen = any(
        isinstance(editorial_tel.get(key), int)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    ai_request_count = editorial_ai_requests
    retries = int(editorial_tel.get("retries") or 0)
    reported_cost = 0.0
    cost_seen = isinstance(editorial_tel.get("provider_reported_cost_usd"), (int, float))
    if cost_seen:
        reported_cost = float(editorial_tel["provider_reported_cost_usd"])

    other_article_inputs = [
        payload.get("article_input")
        for payload in ordered
        if isinstance(payload.get("article_input"), dict)
    ]

    stages = dict(stage_timings or {})
    qa_started = perf_counter()
    states: list[dict[str, Any]] = []
    for index, payload in enumerate(ordered, start=1):
        state: dict[str, Any] = {
            "event_id": payload["event_id"],
            "story_index": index,
            "story_count": len(ordered),
            "original_rank": payload.get("original_rank"),
            "viability_status": payload.get("viability_status"),
            "backfill": payload.get("backfill"),
            "article_input": payload.get("article_input"),
            "article": payload.get("article"),
            "headline": None,
            "dek": None,
            "category": payload.get("category"),
            "source_count": payload.get("source_count"),
            "article_url": payload.get("article_url"),
            "qa_result": None,
            "qa_publishable": False,
            "deliverable": False,
            "skip_reason": None,
            "image": None,
            "final_image_path": payload.get("final_image_path"),
            "telegram": None,
            "generation_failure": payload.get("generation_failure"),
            "evidence_event_id": (payload.get("article_input") or {}).get("event_id"),
            "evidence_sufficiency": payload.get("evidence_sufficiency")
            or (payload.get("article_input") or {}).get("evidence_sufficiency"),
        }
        try:
            preset_skip = payload.get("skip_reason")
            has_article = isinstance(state["article"], dict) and (
                state["article"].get("article_body") or state["article"].get("headline")
            )
            if preset_skip and not has_article:
                state["skip_reason"] = preset_skip
                state["generation_failure"] = payload.get("generation_failure")
                states.append(state)
                continue
            article = state["article"]
            if isinstance(article, dict) and not (article.get("article_body") or article.get("headline")):
                article = None
                state["article"] = None
            if article is None and article_fn is not None:
                if not live_llm:
                    raise BatchError("live_llm_disabled", "article_fn requires live_llm=True")
                generated = article_fn(payload)
                ai_request_count += int(generated.get("http_request_count") or generated.get("ai_request_count") or 1)
                retries += int(generated.get("retry_count") or 0)
                article = generated.get("parsed_output") or generated.get("article")
                tel = generated.get("telemetry") or {}
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = tel.get(key)
                    if isinstance(value, int):
                        token_seen = True
                        if key == "prompt_tokens":
                            prompt_tokens += value
                        elif key == "completion_tokens":
                            completion_tokens += value
                        else:
                            total_tokens += value
                cost = tel.get("provider_reported_cost_usd") or generated.get("provider_reported_cost_usd")
                if isinstance(cost, (int, float)):
                    reported_cost += float(cost)
                    cost_seen = True
                if generated.get("qa_result"):
                    state["qa_result"] = generated["qa_result"]
            if article is None:
                state["skip_reason"] = "missing_article"
                states.append(state)
                continue
            if (article.get("event_id") if isinstance(article, dict) else None) not in {None, payload["event_id"]}:
                state["skip_reason"] = "cross_story_article_event_id"
                states.append(state)
                continue
            state["article"] = article
            state["headline"] = article.get("headline") if isinstance(article, dict) else None
            state["dek"] = article.get("dek") if isinstance(article, dict) else None
            if isinstance(article, dict) and article.get("category"):
                state["category"] = article.get("category")
            article_input = payload.get("article_input") or {}
            if article_input.get("event_id") not in {None, payload["event_id"]}:
                state["skip_reason"] = "cross_story_evidence"
                states.append(state)
                continue
            try:
                qa_result = qa(
                    article,
                    article_input,
                    article_mode="normal",
                    other_article_inputs=other_article_inputs,
                )
            except TypeError:
                try:
                    qa_result = qa(article, article_input, article_mode="normal")
                except TypeError:
                    qa_result = qa(article, article_input)
            state["qa_result"] = qa_result
            publishable = bool(isinstance(qa_result, dict) and qa_result.get("publishable"))
            state["qa_publishable"] = publishable
            if not publishable:
                state["skip_reason"] = _qa_reason(qa_result)
            states.append(state)
        except Exception as exc:
            state["skip_reason"] = f"story_error: {exc.__class__.__name__}: {exc}"
            states.append(state)

    qa_ms = int(round((perf_counter() - qa_started) * 1000))
    returned_articles = {
        row["event_id"]: row.get("article")
        for row in states
        if isinstance(row.get("article"), dict)
        and (row["article"].get("article_body") or row["article"].get("headline"))
    }
    failed_ids = {
        row["event_id"]: str(row.get("skip_reason") or "failed")
        for row in states
        if row["event_id"] not in returned_articles
    }
    completeness = account_requested_ids(selected, returned_articles, failed_ids)
    if not completeness["ok"]:
        for event_id in completeness["missing_ids"]:
            for row in states:
                if row["event_id"] == event_id and not row.get("skip_reason"):
                    row["skip_reason"] = "unaccounted_event"

    image_started = perf_counter()
    image_jobs = [
        {
            "event_id": row["event_id"],
            "story_index": row["story_index"],
            "article": row.get("article"),
            "article_input": row.get("article_input"),
            "primary_url": row.get("article_url"),
        }
        for row in states
        if row.get("qa_publishable")
    ]
    image_results: list[dict[str, Any]] = []
    image_request_count = 0
    if image_fn is not None and image_jobs:
        image_results = run_image_jobs(
            image_jobs,
            image_fn,
            parallel=bool(parallel_images) and not live_image,
        )
    by_image = {item.get("event_id"): item for item in image_results}
    for row in states:
        if not row.get("qa_publishable"):
            continue
        imaged = by_image.get(row["event_id"])
        if imaged is None and row.get("final_image_path"):
            row["image"] = {"success": True, "reused": True, "final_path": row["final_image_path"]}
            continue
        if imaged is None:
            row["skip_reason"] = row.get("skip_reason") or "image_skipped_no_provider"
            continue
        row["image"] = imaged
        image_request_count += int(imaged.get("image_request_count") or 0)
        if imaged.get("success") and (imaged.get("final_path") or imaged.get("final_image_path")):
            row["final_image_path"] = imaged.get("final_path") or imaged.get("final_image_path")
        else:
            row["skip_reason"] = imaged.get("reason") or "image_failed"

    for row in states:
        has_image = bool(row.get("final_image_path"))
        if row.get("qa_publishable") and has_image and not row.get("skip_reason"):
            row["deliverable"] = True
        elif row.get("qa_publishable") and not has_image:
            row["skip_reason"] = row.get("skip_reason") or "missing_image"
            row["deliverable"] = False

    images_ms = int(round((perf_counter() - image_started) * 1000))
    telegram_started = perf_counter()
    telegram_result = None
    if telegram_config is not None:
        telegram_result = deliver_top5_batch(
            states,
            telegram_config,
            transport=telegram_transport,
            live_test=live_telegram,
            persist=persist,
            persist_root=persist_root,
            repo_root=repo_root,
        )
        send_by_id = {
            item.get("event_id"): item
            for item in telegram_result.get("sends") or []
            if item.get("kind") != "batch_header"
        }
        for row in states:
            row["telegram"] = send_by_id.get(row["event_id"])

    telegram_ms = int(round((perf_counter() - telegram_started) * 1000))
    publishable_ids = [row["event_id"] for row in states if row.get("qa_publishable")]
    failed = [
        {"event_id": row["event_id"], "reason": row.get("skip_reason")}
        for row in states
        if row.get("skip_reason")
    ]
    local_elapsed = int(round((perf_counter() - started) * 1000))
    editorial_ms = int(editorial_tel.get("latency_ms") or stages.get("editorial_ms") or 0)
    discovery_ms = int(stages.get("discovery_ms") or 0)
    enrichment_ms = int(stages.get("enrichment_ms") or 0)
    if pipeline_started_at is not None:
        elapsed = int(round((perf_counter() - pipeline_started_at) * 1000))
        timing_basis = "pipeline_wall_clock"
    else:
        elapsed = discovery_ms + enrichment_ms + editorial_ms + qa_ms + images_ms + telegram_ms
        if elapsed < local_elapsed:
            elapsed = local_elapsed
        timing_basis = "sum_of_stages"
    telegram_sends = int((telegram_result or {}).get("telegram_sends") or 0)
    telemetry = build_batch_telemetry(
        batch_run_id=batch_run_id,
        selected_stories=selected,
        publishable_stories=publishable_ids,
        failed_stories=failed,
        skipped_stories=failed,
        total_elapsed_ms=elapsed,
        discovery_ms=discovery_ms,
        enrichment_ms=enrichment_ms,
        editorial_ms=editorial_ms,
        qa_ms=qa_ms,
        images_ms=images_ms,
        telegram_ms=telegram_ms,
        total_timing_basis=timing_basis,
        viability=viability,
        ai_request_count=ai_request_count,
        prompt_tokens=prompt_tokens if token_seen else None,
        completion_tokens=completion_tokens if token_seen else None,
        total_tokens=total_tokens if token_seen else None,
        image_request_count=image_request_count,
        telegram_sends=telegram_sends,
        retries=retries,
        provider_reported_cost_usd=reported_cost if cost_seen else None,
        estimated_list_price_usd=estimated_list_price_usd(
            str(editorial_tel.get("model") or "openai/gpt-oss-120b"),
            prompt_tokens if token_seen else None,
            completion_tokens if token_seen else None,
        ),
        timestamp_utc=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        editorial=editorial_tel,
        stories=[
            {
                "event_id": row["event_id"],
                "story_index": row["story_index"],
                "qa_publishable": row.get("qa_publishable"),
                "deliverable": row.get("deliverable"),
                "skip_reason": row.get("skip_reason"),
                "qa_result": row.get("qa_result"),
                "image": row.get("image"),
                "telegram_success": bool((row.get("telegram") or {}).get("success")),
            }
            for row in states
        ],
    )
    result = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_run_id": batch_run_id,
        "selected_event_ids": selected,
        "editorial_output": editorial_output,
        "editorial_ai_requests": editorial_ai_requests,
        "provider_telemetry": editorial_tel,
        "stories": states,
        "telegram": telegram_result,
        "telemetry": telemetry,
        "live_llm": live_llm,
        "live_image": live_image,
        "live_telegram": live_telegram,
        "parallel_images": parallel_images,
        "image_parallel_ready": True,
    }
    if persist:
        root = persist_root or DEFAULT_RUNS_ROOT
        run_dir = root / batch_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json_utf8(run_dir / "batch.json", redact_secrets(result))
        write_json_utf8(run_dir / "telemetry.json", redact_secrets(telemetry))
        result["artifact_dir"] = str(run_dir)
    return result
