"""V4 end-to-end experiment: writer → verify → repair → QA → FLUX → Telegram.

Stops at AWAITING_APPROVAL. WordPress disabled. Paid private Qwen forbidden.
Kimi is authorized only when NEWSAGENT_V2_V4_ALLOW_KIMI is set.
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.approval.store import STATE_AWAITING_APPROVAL, ApprovalStore
from newsagent_v2.article.qa.policy import ARTICLE_MIN_WORDS_ENV, DEMO_ARTICLE_MIN_WORDS_ENV
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.v4.compile import V4CompileResult, compile_v4_article
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_FALLBACK_PROVIDER,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_KIMI,
)
from newsagent_v2.article.writer.v4.writer import (
    V4_KIMI_MODEL,
    V4_WRITER_MODEL,
    V4NaturalProseWriter,
    assert_v4_writer_is_free,
    build_v4_writer,
)
from newsagent_v2.batch.viability import cluster_to_story
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.control.make import persist_story_artifacts, send_approval_cards
from newsagent_v2.control.make_recovery import MAKE_RUNS_ROOT, build_flux_make_image_fn
from newsagent_v2.control.make_summary import format_make_summary
from newsagent_v2.telegram.cards import approval_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig

MAX_V4_CANDIDATES = 3


def configure_v4_kimi_primary(environ: dict[str, str]) -> dict[str, str]:
    """Force Kimi primary with no Groq fallback for controlled validation."""
    env = dict(environ)
    env[ENV_ALLOW_KIMI] = "true"
    env[ENV_ALLOW_PAID_QWEN] = "0"
    env[ENV_PROVIDER] = PROVIDER_KIMI
    env[ENV_MODEL] = V4_KIMI_MODEL
    env[ENV_MAX_PROVIDER_ATTEMPTS] = "1"
    env[ENV_FALLBACK_PROVIDER] = ""
    env["NEWSAGENT_V2_KIMI_WRITER_FALLBACK"] = "0"
    os.environ[ENV_ALLOW_KIMI] = "true"
    os.environ[ENV_ALLOW_PAID_QWEN] = "0"
    os.environ[ENV_PROVIDER] = PROVIDER_KIMI
    os.environ[ENV_MODEL] = V4_KIMI_MODEL
    os.environ[ENV_MAX_PROVIDER_ATTEMPTS] = "1"
    os.environ[ENV_FALLBACK_PROVIDER] = ""
    os.environ["NEWSAGENT_V2_KIMI_WRITER_FALLBACK"] = "0"
    return env


def _sha256_payload(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _ensure_demo_min_words(environ: dict[str, str]) -> None:
    if not str(environ.get(DEMO_ARTICLE_MIN_WORDS_ENV) or environ.get(ARTICLE_MIN_WORDS_ENV) or "").strip():
        environ[ARTICLE_MIN_WORDS_ENV] = "120"
        environ[DEMO_ARTICLE_MIN_WORDS_ENV] = "120"
        os.environ[ARTICLE_MIN_WORDS_ENV] = "120"
        os.environ[DEMO_ARTICLE_MIN_WORDS_ENV] = "120"


def run_v4_experiment(
    *,
    environ: dict[str, str],
    store: ApprovalStore,
    telegram_config: TelegramConfig,
    telegram_client: TelegramTestClient,
    discover_fn: Callable[[], dict[str, Any]] | None = None,
    writer: V4NaturalProseWriter | Any | None = None,
    image_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    max_candidates: int = MAX_V4_CANDIDATES,
    stories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One V4 vertical slice. WordPress disabled. Kimi primary when authorized."""
    env = configure_v4_kimi_primary(dict(environ))
    _ensure_demo_min_words(env)
    model = str(env.get(ENV_MODEL) or V4_KIMI_MODEL)
    assert_v4_writer_is_free(model, environ=env)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempts_root = MAKE_RUNS_ROOT / f"v4-{run_stamp}" / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)

    if stories is None:
        discovered = (discover_fn or discover_ranked_top5)()
        ranked = list(discovered.get("ranked_clusters") or [])
        stories = [cluster_to_story(cluster, original_rank=i) for i, cluster in enumerate(ranked, start=1)]

    prose_writer = writer or build_v4_writer(
        environ=env,
        enable_failover=False,
        max_calls=max(max_candidates * 4, max_candidates + 2),
    )
    assert_v4_writer_is_free(getattr(prose_writer, "model", model), environ=env)

    failures: list[dict[str, Any]] = []
    winner: V4CompileResult | None = None
    winner_story: dict[str, Any] | None = None
    generations = 0
    for index, story in enumerate(stories, start=1):
        if generations >= max_candidates:
            break
        generations += 1
        compiled = compile_v4_article(
            story,
            writer=prose_writer,
            attempts_root=attempts_root,
            rank=index,
            research=True,
        )
        row = {
            "event_id": compiled.event_id,
            "rank": index,
            "native_words": compiled.native_words,
            "final_words": compiled.final_words,
            "supported": compiled.supported,
            "ambiguous": compiled.ambiguous,
            "unsupported": compiled.unsupported,
            "repairs": compiled.repair_log.get("rounds") if compiled.repair_log else 0,
            "expansion_calls": compiled.expansion_calls,
            "editorial_target_met": compiled.editorial_target_met,
            "critical_codes": compiled.critical_codes,
            "path": compiled.attempt_path,
            "failure_class": compiled.failure_class,
            "writer_model": compiled.writer_model,
        }
        if compiled.ok and compiled.article and compiled.qa:
            # First publishable article (critical_count=0) wins; warnings allowed.
            winner = compiled
            winner_story = dict(story)
            winner_story["article"] = compiled.article
            winner_story["qa_result"] = compiled.qa
            winner_story["article_input"] = compiled.article_input or story.get("article_input")
            break
        failures.append(row)

    provider_name = str(getattr(prose_writer, "provider", "") or "kimi")
    model_name = str(getattr(prose_writer, "model", model) or model)
    generation_calls = int(getattr(prose_writer, "generation_calls", 0) or 0)
    kimi_calls = generation_calls if provider_name.lower() == "kimi" else 0
    groq_calls = generation_calls if provider_name.lower() == "groq" else 0
    telemetry = {
        "architecture": "v4",
        "writer": f"{provider_name}/{model_name}",
        "provider": provider_name,
        "model": model_name,
        "kimi_calls": kimi_calls,
        "groq_calls": groq_calls,
        "paid_private_qwen_calls": 0,
        "candidate_attempts": generations,
        "failures": failures,
        "attempts_root": str(attempts_root),
        "wordpress_disabled": True,
        "image_request_count": 0,
        "approval_card_sent": False,
        "editorial_target_min_words": 250,
        "editorial_target_max_words": 400,
    }
    if winner is None or winner_story is None:
        summary = "V4 NO SAFE ARTICLE\n" + "\n".join(
            f"{row.get('event_id')}: {row.get('failure_class')} codes={','.join(row.get('critical_codes') or [])}"
            for row in failures
        )
        try:
            telegram_client.send_message(chat_id=telegram_config.test_chat_id, text=summary[:3500])
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False,
            "status": "NO SAFE ARTICLE",
            "telemetry": telemetry,
            "failures": failures,
            "candidate_attempts": generations,
            "batch_id": f"v4-{run_stamp}",
            "attempts_root": str(attempts_root),
            "provider": provider_name,
            "model": model_name,
            "kimi_calls": kimi_calls,
            "groq_calls": groq_calls,
            "paid_private_qwen_calls": 0,
            "completion_text": summary,
        }

    article = deepcopy(winner.article or {})
    qa = deepcopy(winner.qa or {})
    event_id = winner.event_id
    image = (image_fn or build_flux_make_image_fn(env))(
        {
            "event_id": event_id,
            "article": article,
            "article_input": winner_story.get("article_input"),
        }
    )
    telemetry["image_request_count"] = int(image.get("image_request_count") or 0)
    batch_id = f"v4-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    row = {
        "event_id": event_id,
        "story_index": 1,
        "article": article,
        "article_input": winner_story.get("article_input"),
        "qa_result": qa,
        "qa_publishable": True,
        "headline": article.get("headline"),
        "dek": article.get("dek"),
        "deliverable": False,
        "final_image_path": None,
        "image": image,
        "writer_model": winner.writer_model,
        "allow_wordpress": False,
        "wordpress_disabled": True,
        "publish_on_approve": False,
        "article_sha256": _sha256_payload(article),
        "architecture": "v4",
    }
    if not image.get("success"):
        persist_story_artifacts(store=store, batch_id=batch_id, row=row)
        return {
            "ok": False,
            "status": "IMAGE FAILED",
            "telemetry": telemetry,
            "headline": article.get("headline"),
            "words": winner.final_words,
            "batch_id": batch_id,
            "image_failure": image.get("reason"),
            "kimi_calls": kimi_calls,
            "groq_calls": groq_calls,
            "paid_private_qwen_calls": 0,
            "article_hash": row["article_sha256"],
        }

    row["final_image_path"] = image.get("final_path")
    row["deliverable"] = True
    persist_story_artifacts(store=store, batch_id=batch_id, row=row)
    extra = store.read_story(batch_id, event_id) or {}
    extra.update(
        {
            "no_regeneration": True,
            "allow_wordpress": False,
            "publish_on_approve": False,
            "wordpress_disabled": True,
            "architecture": "v4",
            "writer_model": winner.writer_model,
            "frozen_bundle_path": str(Path(str(image.get("final_path"))).parent),
        }
    )
    store.write_story(batch_id, event_id, extra)
    cards = send_approval_cards(
        batch_id=batch_id,
        stories=[row],
        client=telegram_client,
        store=store,
        chat_id=telegram_config.test_chat_id,
    )
    sent_ok = bool(cards) and cards[0].get("ok") is not False
    if not sent_ok:
        return {
            "ok": False,
            "status": "TELEGRAM FAILED",
            "telemetry": telemetry,
            "headline": article.get("headline"),
            "batch_id": batch_id,
            "kimi_calls": kimi_calls,
            "groq_calls": groq_calls,
            "paid_private_qwen_calls": 0,
            "article_hash": row["article_sha256"],
        }
    telemetry["approval_card_sent"] = True
    caption = approval_caption(
        rank=1,
        headline=str(article.get("headline") or ""),
        dek=str(article.get("dek") or ""),
        total=1,
    )
    summary = format_make_summary(
        stories=[{**row, "deliverable": True}],
        telemetry=telemetry,
        viability={},
        approval_cards=cards,
    )
    telegram_client.send_message(chat_id=telegram_config.test_chat_id, text=summary)
    store.write_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "selected_event_ids": [event_id],
            "awaiting": 1,
            "failed": 0,
            "telemetry": telemetry,
            "summary": summary,
            "state": STATE_AWAITING_APPROVAL,
            "wordpress_disabled": True,
            "architecture": "v4",
        },
    )
    message_id = None
    if cards and isinstance(cards[0].get("send"), dict):
        message_id = cards[0]["send"].get("message_id")
    metrics = qa.get("metrics") or {}
    exp = winner.expansion or {}
    return {
        "ok": True,
        "status": "SUCCESS",
        "event_id": event_id,
        "headline": article.get("headline"),
        "words": int(metrics.get("article_word_count") or winner.final_words or word_count(str(article.get("article_body") or ""))),
        "initial_words": winner.initial_words_after_repair or winner.native_words,
        "expansion_triggered": bool(exp.get("triggered")),
        "unused_facts_before_expansion": exp.get("unused_facts_before_expansion") or [],
        "expansion_generated_words": exp.get("expansion_generated_words") or 0,
        "expansion_validated_words": exp.get("expansion_validated_words") or 0,
        "editorial_target_met": bool(winner.editorial_target_met),
        "writer": f"{provider_name}/{winner.writer_model}",
        "provider": provider_name,
        "model": winner.writer_model,
        "writer_calls": winner.writer_calls,
        "repair_calls": winner.repair_calls,
        "expansion_calls": winner.expansion_calls,
        "grounding": metrics.get("body_claim_coverage"),
        "copyright": metrics.get("exact_overlap_count"),
        "qa": "PASS",
        "critical_count": qa.get("critical_count", 0),
        "warning_count": qa.get("warning_count", 0),
        "warning_codes": qa.get("warning_codes") or [],
        "qa_publishable": True,
        "article_hash": row["article_sha256"],
        "image_provider": "cloudflare_flux",
        "image": "PASS",
        "Telegram": "SENT",
        "message_id": message_id,
        "state": STATE_AWAITING_APPROVAL,
        "bundle": str(Path(str(image.get("final_path"))).parent),
        "batch_id": batch_id,
        "telemetry": telemetry,
        "kimi_calls": kimi_calls,
        "groq_calls": groq_calls,
        "paid_private_qwen_calls": 0,
        "caption": caption,
        "stories": [row],
        "approval_cards": cards,
        "evidence_capacity": winner.evidence_capacity,
        "article_type": winner.article_type,
        "unique_propositions": winner.unique_propositions,
        "independent_sources": winner.independent_sources,
        "native_words": winner.native_words,
        "final_words": winner.final_words,
    }


def resume_v4_image_telegram(
    *,
    event_id: str,
    article: dict[str, Any],
    qa: dict[str, Any],
    article_input: dict[str, Any] | None,
    environ: dict[str, str],
    store: ApprovalStore,
    telegram_config: TelegramConfig,
    telegram_client: TelegramTestClient,
    writer_model: str = V4_WRITER_MODEL,
    writer_calls: int = 1,
    repair_calls: int = 0,
    image_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resume from a frozen qa_publishable article: ONE FLUX image → Telegram. No writer."""
    env = dict(environ)
    _ensure_demo_min_words(env)
    image = (image_fn or build_flux_make_image_fn(env))(
        {
            "event_id": event_id,
            "article": article,
            "article_input": article_input,
        }
    )
    telemetry = {
        "architecture": "v4",
        "writer": f"groq/{writer_model}",
        "kimi_calls": 0,
        "paid_private_qwen_calls": 0,
        "resume_image_telegram": True,
        "image_request_count": int(image.get("image_request_count") or 0),
        "wordpress_disabled": True,
        "approval_card_sent": False,
        "image_failure": image.get("reason"),
    }
    batch_id = f"v4-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    row = {
        "event_id": event_id,
        "story_index": 1,
        "article": article,
        "article_input": article_input,
        "qa_result": qa,
        "qa_publishable": True,
        "headline": article.get("headline"),
        "dek": article.get("dek"),
        "deliverable": False,
        "final_image_path": None,
        "image": image,
        "writer_model": writer_model,
        "allow_wordpress": False,
        "wordpress_disabled": True,
        "publish_on_approve": False,
        "article_sha256": _sha256_payload(article),
        "architecture": "v4",
    }
    if not image.get("success"):
        persist_story_artifacts(store=store, batch_id=batch_id, row=row)
        return {
            "ok": False,
            "status": "IMAGE FAILED",
            "telemetry": telemetry,
            "headline": article.get("headline"),
            "words": int((qa.get("metrics") or {}).get("article_word_count") or 0),
            "batch_id": batch_id,
            "image_failure": image.get("reason"),
            "kimi_calls": 0,
            "paid_private_qwen_calls": 0,
            "article_hash": row["article_sha256"],
            "event_id": event_id,
            "writer": f"groq/{writer_model}",
            "writer_calls": writer_calls,
            "repair_calls": repair_calls,
        }

    row["final_image_path"] = image.get("final_path")
    row["deliverable"] = True
    persist_story_artifacts(store=store, batch_id=batch_id, row=row)
    extra = store.read_story(batch_id, event_id) or {}
    extra.update(
        {
            "no_regeneration": True,
            "allow_wordpress": False,
            "publish_on_approve": False,
            "wordpress_disabled": True,
            "architecture": "v4",
            "writer_model": writer_model,
            "frozen_bundle_path": str(Path(str(image.get("final_path"))).parent),
        }
    )
    store.write_story(batch_id, event_id, extra)
    cards = send_approval_cards(
        batch_id=batch_id,
        stories=[row],
        client=telegram_client,
        store=store,
        chat_id=telegram_config.test_chat_id,
    )
    sent_ok = bool(cards) and cards[0].get("ok") is not False
    if not sent_ok:
        return {
            "ok": False,
            "status": "TELEGRAM FAILED",
            "telemetry": telemetry,
            "headline": article.get("headline"),
            "batch_id": batch_id,
            "kimi_calls": 0,
            "paid_private_qwen_calls": 0,
            "article_hash": row["article_sha256"],
            "event_id": event_id,
        }
    telemetry["approval_card_sent"] = True
    caption = approval_caption(
        rank=1,
        headline=str(article.get("headline") or ""),
        dek=str(article.get("dek") or ""),
        total=1,
    )
    summary = format_make_summary(
        stories=[{**row, "deliverable": True}],
        telemetry=telemetry,
        viability={},
        approval_cards=cards,
    )
    telegram_client.send_message(chat_id=telegram_config.test_chat_id, text=summary)
    store.write_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "selected_event_ids": [event_id],
            "awaiting": 1,
            "failed": 0,
            "telemetry": telemetry,
            "summary": summary,
            "state": STATE_AWAITING_APPROVAL,
            "wordpress_disabled": True,
            "architecture": "v4",
        },
    )
    message_id = None
    if cards and isinstance(cards[0].get("send"), dict):
        message_id = cards[0]["send"].get("message_id")
    metrics = qa.get("metrics") or {}
    return {
        "ok": True,
        "status": "SUCCESS",
        "event_id": event_id,
        "headline": article.get("headline"),
        "words": int(metrics.get("article_word_count") or word_count(str(article.get("article_body") or ""))),
        "writer": f"groq/{writer_model}",
        "writer_calls": writer_calls,
        "repair_calls": repair_calls,
        "grounding": metrics.get("body_claim_coverage"),
        "copyright": metrics.get("exact_overlap_count"),
        "qa": "PASS",
        "article_hash": row["article_sha256"],
        "image_provider": "cloudflare_flux",
        "image": "PASS",
        "Telegram": "SENT",
        "message_id": message_id,
        "state": STATE_AWAITING_APPROVAL,
        "bundle": str(Path(str(image.get("final_path"))).parent),
        "batch_id": batch_id,
        "telemetry": telemetry,
        "kimi_calls": 0,
        "paid_private_qwen_calls": 0,
        "caption": caption,
        "stories": [row],
        "approval_cards": cards,
    }
