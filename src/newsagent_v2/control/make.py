"""/make Top-5 generation. Reuses ranking, QA, and image jobs. No live calls by default."""

from __future__ import annotations

import os
import threading
from hashlib import sha256
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Callable

from newsagent_v2.approval.store import (
    STATE_AWAITING_APPROVAL,
    STATE_GENERATED,
    STATE_IMAGE_FAILED,
    STATE_QA_FAILED,
    ApprovalStore,
)
from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.batch.select import select_top5_event_ids
from newsagent_v2.control.make_summary import format_make_summary
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.telegram.cards import approval_caption, approval_keyboard, html_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.contract import ACK_MAKE_TEXT, BUSY_MAKE_TEXT
from newsagent_v2.telegram.full_article_delivery import send_full_frozen_article

MAKE_LOCK = threading.Lock()
MAKE_COOLDOWN_ENV = "NEWSAGENT_V2_MAKE_COOLDOWN_SECONDS"
DEFAULT_MAKE_COOLDOWN_SECONDS = 60
MAKE_COOLDOWN_TEXT = "⏳ A V2 batch just ran. Please wait before starting another."
_last_batch_finished_at: float | None = None
_cooldown_seconds_override: float | None = None
_monotonic = monotonic


def batch_busy() -> bool:
    return MAKE_LOCK.locked()


def reset_make_guard(
    *,
    cooldown_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    """Test helper. Resets in-process /make lock metadata and cooldown."""
    global _last_batch_finished_at, _cooldown_seconds_override, _monotonic
    _last_batch_finished_at = None
    _cooldown_seconds_override = cooldown_seconds
    _monotonic = clock or monotonic


def resolve_make_cooldown_seconds(environ: dict[str, str] | None = None) -> float:
    if _cooldown_seconds_override is not None:
        return float(_cooldown_seconds_override)
    source = environ if environ is not None else os.environ
    raw = source.get(MAKE_COOLDOWN_ENV)
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_MAKE_COOLDOWN_SECONDS)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(DEFAULT_MAKE_COOLDOWN_SECONDS)
    return max(0.0, value)


def make_cooldown_remaining(environ: dict[str, str] | None = None) -> float:
    if _last_batch_finished_at is None:
        return 0.0
    window = resolve_make_cooldown_seconds(environ)
    if window <= 0:
        return 0.0
    elapsed = _monotonic() - _last_batch_finished_at
    return max(0.0, window - elapsed)


def completion_text(*, awaiting: int, failed: int) -> str:
    """Backward-compatible helper. Live /make uses format_make_summary."""
    total = MAKE_STORY_COUNT
    if failed:
        return f"⚠️ V2 PARTIAL — {awaiting}/{total} READY"
    return f"✅ V2 TOP 1 READY — {awaiting}/{total}"


def _hash_obj(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return sha256(value).hexdigest()
    text = str(value)
    return sha256(text.encode("utf-8")).hexdigest()


def send_approval_cards(
    *,
    batch_id: str,
    stories: list[dict[str, Any]],
    client: TelegramTestClient,
    store: ApprovalStore,
    chat_id: str,
) -> list[dict[str, Any]]:
    """Send branded image card, then EXACT frozen full article, with APPROVE/REJECT."""
    sent: list[dict[str, Any]] = []
    deliverable = [row for row in stories if row.get("deliverable")]
    total = len(deliverable)
    for row in deliverable:
        caption = approval_caption(
            rank=int(row["story_index"]),
            headline=str(row.get("headline") or ""),
            dek=row.get("dek"),
            total=total,
            article_type=row.get("article_type_label") or row.get("article_type"),
            body_words=row.get("body_words") or row.get("canonical_body_words"),
        )
        image_path = Path(str(row["final_image_path"]))
        result = client.send_photo(
            chat_id=chat_id,
            photo_name=image_path.name,
            photo_bytes=image_path.read_bytes(),
            caption=html_caption(caption),
            reply_markup=approval_keyboard(batch_id, str(row["event_id"])),
        )
        if not result.get("ok"):
            sent.append({"event_id": row["event_id"], "send": result, "caption": caption, "ok": False})
            continue

        article = row.get("article") if isinstance(row.get("article"), dict) else {}
        body = str(article.get("article_body") or "")
        expected_hash = str(
            row.get("canonical_body_hash") or row.get("article_sha256") or ""
        ) or None
        full = send_full_frozen_article(
            client=client,
            chat_id=chat_id,
            batch_id=batch_id,
            event_id=str(row["event_id"]),
            rank=int(row["story_index"]),
            total=total,
            headline=str(row.get("headline") or article.get("headline") or ""),
            body=body,
            article_type=row.get("article_type_label") or row.get("article_type"),
            expected_hash=expected_hash,
            words=row.get("body_words") or row.get("canonical_body_words"),
        )
        if not full.get("ok"):
            sent.append(
                {
                    "event_id": row["event_id"],
                    "send": result,
                    "full_article": full,
                    "caption": caption,
                    "ok": False,
                }
            )
            continue

        extra = {
            "caption": caption,
            "telegram_image_message_id": result.get("message_id"),
            "telegram_message_id": full.get("telegram_message_id") or result.get("message_id"),
            "telegram_full_article_message_id": full.get("telegram_message_id"),
            "telegram_full_article_parts": full.get("parts"),
            "telegram_full_article_plain": full.get("last_part_plain"),
            "telegram_send_ok": True,
            "full_article_sent": True,
            "canonical_body_hash": full.get("article_hash"),
        }
        store.cas_story_state(
            batch_id,
            str(row["event_id"]),
            expected=STATE_GENERATED,
            new_state=STATE_AWAITING_APPROVAL,
            extra=extra,
        )
        sent.append(
            {
                "event_id": row["event_id"],
                "send": result,
                "full_article": full,
                "caption": caption,
                "ok": True,
            }
        )
    return sent


def persist_story_artifacts(
    *,
    store: ApprovalStore,
    batch_id: str,
    row: dict[str, Any],
) -> None:
    qa_ok = bool(row.get("qa_publishable"))
    has_image = bool(row.get("final_image_path"))
    if not qa_ok:
        state = STATE_QA_FAILED
    elif not has_image:
        state = STATE_IMAGE_FAILED
    else:
        state = STATE_GENERATED
    image_path = row.get("final_image_path")
    image_hash = None
    if image_path and Path(str(image_path)).is_file():
        image_hash = file_sha256(Path(str(image_path)))
    article = row.get("article") if isinstance(row.get("article"), dict) else {}
    store.write_story(
        batch_id,
        str(row["event_id"]),
        {
            "event_id": row["event_id"],
            "batch_id": batch_id,
            "rank": row.get("story_index"),
            "original_rank": row.get("original_rank"),
            "viability_status": row.get("viability_status"),
            "backfill": row.get("backfill"),
            "state": state,
            "article": article,
            "article_sha256": _hash_obj(article.get("article_body") if article else None),
            "headline": row.get("headline") or article.get("headline"),
            "dek": row.get("dek") or article.get("dek"),
            "slug": article.get("slug"),
            "seo_title": article.get("seo_title"),
            "meta_description": article.get("meta_description"),
            "qa_result": row.get("qa_result"),
            "qa_publishable": qa_ok,
            "final_image_path": image_path,
            "image_sha256": image_hash,
            "reference_path": (row.get("image") or {}).get("reference_path"),
            "reference_sha256": (row.get("image") or {}).get("reference_sha256"),
            "image_latency_ms": (row.get("image") or {}).get("latency_ms"),
            "image_http_status": (row.get("image") or {}).get("http_status"),
            "skip_reason": row.get("skip_reason"),
            "generation_failure": row.get("generation_failure"),
            "evidence_sufficiency": row.get("evidence_sufficiency")
            or (row.get("article_input") or {}).get("evidence_sufficiency"),
            "evidence_fetch": [
                {
                    "url": item.get("url"),
                    "source": item.get("source"),
                    "extraction_method": item.get("extraction_method"),
                    "access_blocked": item.get("access_blocked"),
                    "extracted_chars": len(str(item.get("extracted_text") or "")),
                }
                for item in ((row.get("article_input") or {}).get("evidence") or [])
                if isinstance(item, dict)
            ],
            "prompt_compaction": row.get("prompt_compaction"),
            "wp_url": None,
        },
    )


def run_make_generation(
    *,
    event_ids: list[str] | None = None,
    ranked_clusters: Any = None,
    evidence_pack: dict[str, Any] | None = None,
    stories: list[dict[str, Any]],
    editorial_fn: Callable[[list[str]], dict[str, Any]] | None = None,
    content_fn: Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]] | None = None,
    qa_fn: Callable[..., dict[str, Any]] | None = None,
    image_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    store: ApprovalStore,
    telegram_config: TelegramConfig,
    telegram_client: TelegramTestClient,
    parallel_images: bool = True,
    provider_telemetry: dict[str, Any] | None = None,
    pipeline_started_at: float | None = None,
    stage_timings: dict[str, Any] | None = None,
    viability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = select_top5_event_ids(
        ranked_clusters=ranked_clusters,
        evidence_pack=evidence_pack,
        event_ids=event_ids,
    )
    prepared = [dict(row) for row in stories]
    if content_fn is not None:
        articles = content_fn(prepared)
        for row in prepared:
            article = articles.get(row["event_id"]) if isinstance(articles, dict) else None
            if article:
                row["article"] = article
    batch = run_top5_batch(
        event_ids=selected,
        stories=prepared,
        editorial_fn=editorial_fn,
        qa_fn=qa_fn,
        image_fn=image_fn,
        telegram_config=None,
        live_llm=False,
        live_image=False,
        live_telegram=False,
        parallel_images=parallel_images,
        persist=False,
        provider_telemetry=provider_telemetry,
        pipeline_started_at=pipeline_started_at,
        stage_timings=stage_timings,
        viability=viability,
    )
    batch_id = str(batch["batch_run_id"])
    for row in batch["stories"]:
        persist_story_artifacts(store=store, batch_id=batch_id, row=row)
    cards = send_approval_cards(
        batch_id=batch_id,
        stories=batch["stories"],
        client=telegram_client,
        store=store,
        chat_id=telegram_config.test_chat_id,
    )
    awaiting = sum(1 for row in batch["stories"] if row.get("deliverable"))
    failed = max(0, len(batch["stories"]) - awaiting)
    telemetry = dict(batch.get("telemetry") or {})
    telemetry["approval_card_sent"] = any(
        isinstance(row, dict) and row.get("ok") is not False for row in cards
    )
    batch["telemetry"] = telemetry
    summary = format_make_summary(
        stories=batch["stories"],
        telemetry=telemetry,
        viability=viability,
        approval_cards=cards,
    )
    telegram_client.send_message(chat_id=telegram_config.test_chat_id, text=summary)
    store.write_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "selected_event_ids": selected,
            "awaiting": awaiting,
            "failed": failed,
            "telemetry": batch.get("telemetry"),
            "summary": summary,
            "viability": viability or {},
            "final_selected_ids": selected,
        },
    )
    batch["approval_cards"] = cards
    batch["completion_text"] = summary
    batch["batch_id"] = batch_id
    return batch


def execute_make(
    *,
    telegram_config: TelegramConfig,
    client: TelegramTestClient,
    store: ApprovalStore,
    pipeline: Callable[[], dict[str, Any]],
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    global _last_batch_finished_at
    remaining = make_cooldown_remaining(environ)
    if remaining > 0:
        client.send_message(chat_id=telegram_config.test_chat_id, text=MAKE_COOLDOWN_TEXT)
        return {
            "ok": False,
            "busy": False,
            "cooldown": True,
            "cooldown_remaining_seconds": remaining,
            "acked": False,
        }
    acquired = MAKE_LOCK.acquire(blocking=False)
    if not acquired:
        client.send_message(chat_id=telegram_config.test_chat_id, text=BUSY_MAKE_TEXT)
        return {"ok": False, "busy": True, "acked": False}
    remaining = make_cooldown_remaining(environ)
    if remaining > 0:
        MAKE_LOCK.release()
        client.send_message(chat_id=telegram_config.test_chat_id, text=MAKE_COOLDOWN_TEXT)
        return {
            "ok": False,
            "busy": False,
            "cooldown": True,
            "cooldown_remaining_seconds": remaining,
            "acked": False,
        }
    started = perf_counter()
    try:
        client.send_message(chat_id=telegram_config.test_chat_id, text=ACK_MAKE_TEXT)
        result = pipeline()
        result["acked"] = True
        result["busy"] = False
        result["ok"] = True
        result["elapsed_ms"] = int((perf_counter() - started) * 1000)
        return result
    finally:
        _last_batch_finished_at = _monotonic()
        MAKE_LOCK.release()
