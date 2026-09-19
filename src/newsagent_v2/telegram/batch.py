"""TEST-only Top-5 Telegram batch. One message per story plus an optional header."""

from __future__ import annotations

from typing import Any, Callable

from newsagent_v2.batch.contract import BATCH_HEADER_EVENT_ID, BATCH_HEADER_TEXT, TOP5_COUNT
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.contract import KIND_BATCH_HEADER, KIND_STORY, TelegramOutbound
from newsagent_v2.telegram.delivery import deliver_test_message


def batch_header_payload() -> TelegramOutbound:
    return TelegramOutbound(
        event_id=BATCH_HEADER_EVENT_ID,
        headline=BATCH_HEADER_TEXT,
        kind=KIND_BATCH_HEADER,
        mode="test",
    )


def story_outbound(
    *,
    event_id: str,
    headline: str,
    dek: str | None,
    category: str | None,
    source_count: int | None,
    image_path: str | None,
    story_index: int,
    story_count: int = TOP5_COUNT,
    article_url: str | None = None,
) -> TelegramOutbound:
    return TelegramOutbound(
        event_id=event_id,
        headline=headline,
        dek=dek,
        category=category,
        source_count=source_count,
        image_path=image_path,
        article_url=article_url,
        kind=KIND_STORY,
        story_index=story_index,
        story_count=story_count,
        mode="test",
    )


def deliver_top5_batch(
    stories: list[dict[str, Any]],
    config: TelegramConfig,
    *,
    transport: Callable[..., Any] | None = None,
    live_test: bool = False,
    persist: bool = False,
    persist_root: Any = None,
    repo_root: Any = None,
    send_header: bool = True,
) -> dict[str, Any]:
    """
    Sequential TEST sends. Does not send full article bodies.
    Skips stories that are not deliverable. Does not abort the batch.
    """
    if not config.test_mode:
        raise ValueError("production Telegram is impossible: test_mode must be True")

    sends: list[dict[str, Any]] = []
    header_result = None
    deliverable = [row for row in stories if row.get("deliverable")]
    if send_header and deliverable:
        header_result = deliver_test_message(
            batch_header_payload(),
            config,
            transport=transport,
            live_test=live_test,
            persist=persist,
            persist_root=persist_root,
            repo_root=repo_root,
        )
        sends.append(
            {
                "kind": KIND_BATCH_HEADER,
                "event_id": BATCH_HEADER_EVENT_ID,
                "success": bool(header_result.get("success")),
                "result": header_result,
            }
        )

    for row in stories:
        if not row.get("deliverable"):
            sends.append(
                {
                    "kind": KIND_STORY,
                    "event_id": row.get("event_id"),
                    "story_index": row.get("story_index"),
                    "success": False,
                    "skipped": True,
                    "reason": row.get("skip_reason") or "not_deliverable",
                }
            )
            continue
        payload = story_outbound(
            event_id=str(row["event_id"]),
            headline=str(row.get("headline") or ""),
            dek=row.get("dek"),
            category=row.get("category"),
            source_count=row.get("source_count"),
            image_path=row.get("final_image_path"),
            story_index=int(row["story_index"]),
            story_count=int(row.get("story_count") or TOP5_COUNT),
            article_url=row.get("article_url"),
        )
        result = deliver_test_message(
            payload,
            config,
            transport=transport,
            live_test=live_test,
            persist=persist,
            persist_root=persist_root,
            repo_root=repo_root,
        )
        sends.append(
            {
                "kind": KIND_STORY,
                "event_id": row.get("event_id"),
                "story_index": row.get("story_index"),
                "success": bool(result.get("success")),
                "skipped": False,
                "result": result,
                "text": (result.get("formatted_payload") or {}).get("text"),
            }
        )

    telegram_sends = sum(1 for item in sends if item.get("result") and not item.get("skipped"))
    telegram_success = sum(
        1 for item in sends if item.get("result") and item.get("success") and not item.get("skipped")
    )
    return {
        "header": header_result,
        "sends": sends,
        "telegram_sends": telegram_sends,
        "telegram_success": telegram_success,
        "live_test": bool(live_test),
        "test_mode": True,
    }
