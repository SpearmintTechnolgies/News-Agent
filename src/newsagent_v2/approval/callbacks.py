"""Idempotent approve/reject. Publishes frozen artifacts only."""

from __future__ import annotations

from typing import Any, Callable

from newsagent_v2.approval.store import (
    STATE_APPROVED,
    STATE_AWAITING_APPROVAL,
    STATE_PUBLISHED,
    STATE_PUBLISHING,
    STATE_PUBLISH_FAILED,
    STATE_REJECTED,
    ApprovalStore,
)
from newsagent_v2.telegram.cards import (
    APPROVE_PREFIX,
    REJECT_PREFIX,
    empty_keyboard,
    parse_callback_data,
)
from newsagent_v2.wordpress.adapter import WordPressPublishError, publish_frozen_story, sanitize_wp_error
from newsagent_v2.wordpress.config import WordPressConfig


def handle_callback(
    raw_data: str,
    *,
    store: ApprovalStore,
    wp_config: WordPressConfig | None = None,
    wp_transport: Callable[..., Any] | None = None,
    publish_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parsed = parse_callback_data(raw_data)
    if parsed is None:
        return {"ok": False, "reason": "invalid_callback"}
    batch_id = parsed["batch_id"]
    event_id = parsed["event_id"]
    story = store.read_story(batch_id, event_id)
    if story is None:
        return {"ok": False, "reason": "unknown_story", "batch_id": batch_id, "event_id": event_id}

    if parsed["action"] == REJECT_PREFIX:
        if story.get("state") in {STATE_PUBLISHED, STATE_PUBLISHING, STATE_APPROVED}:
            return {
                "ok": True,
                "action": "reject_ignored",
                "state": story.get("state"),
                "batch_id": batch_id,
                "event_id": event_id,
                "url": story.get("wp_url"),
                "telegram_caption_suffix": None,
            }
        updated = store.cas_story_state(
            batch_id,
            event_id,
            expected={STATE_AWAITING_APPROVAL, STATE_REJECTED, STATE_PUBLISH_FAILED},
            new_state=STATE_REJECTED,
        )
        return {
            "ok": True,
            "action": "reject",
            "state": STATE_REJECTED,
            "published": False,
            "batch_id": batch_id,
            "event_id": event_id,
            "message_id": (updated or story).get("telegram_message_id"),
            "caption": story.get("caption") or "",
            "telegram_caption_suffix": "❌ Rejected",
            "reply_markup": empty_keyboard(),
        }

    if parsed["action"] != APPROVE_PREFIX:
        return {"ok": False, "reason": "invalid_callback"}

    if story.get("state") in {STATE_PUBLISHED, STATE_APPROVED}:
        return {
            "ok": True,
            "action": "approve_idempotent",
            "state": story.get("state"),
            "published": story.get("state") == STATE_PUBLISHED,
            "duplicate": True,
            "url": story.get("wp_url"),
            "batch_id": batch_id,
            "event_id": event_id,
            "message_id": story.get("telegram_message_id"),
            "telegram_caption_suffix": None,
            "wordpress_disabled": bool(story.get("wordpress_disabled")),
        }
    if story.get("state") == STATE_PUBLISHING:
        return {
            "ok": True,
            "action": "approve_in_progress",
            "state": STATE_PUBLISHING,
            "published": False,
            "duplicate": True,
            "batch_id": batch_id,
            "event_id": event_id,
        }
    if story.get("state") == STATE_REJECTED:
        return {
            "ok": True,
            "action": "approve_ignored_rejected",
            "state": STATE_REJECTED,
            "published": False,
            "batch_id": batch_id,
            "event_id": event_id,
        }
    claimed = store.cas_story_state(
        batch_id,
        event_id,
        expected=STATE_AWAITING_APPROVAL,
        new_state=STATE_PUBLISHING,
    )
    if claimed is None or claimed.get("state") != STATE_PUBLISHING:
        latest = store.read_story(batch_id, event_id) or story
        return {
            "ok": True,
            "action": "approve_idempotent",
            "state": latest.get("state"),
            "published": latest.get("state") == STATE_PUBLISHED,
            "duplicate": True,
            "url": latest.get("wp_url"),
            "batch_id": batch_id,
            "event_id": event_id,
        }

    try:
        publisher = publish_fn or publish_frozen_story
        kwargs: dict[str, Any] = {
            "article": claimed.get("article") or {},
            "image_path": claimed.get("final_image_path"),
        }
        if publish_fn is None:
            if wp_config is None or wp_transport is None:
                raise WordPressPublishError("wp_unconfigured", "WordPress is not configured")
            kwargs["config"] = wp_config
            kwargs["transport"] = wp_transport
        result = publisher(**kwargs)
        if result.get("wordpress_disabled") or result.get("held"):
            store.cas_story_state(
                batch_id,
                event_id,
                expected=STATE_PUBLISHING,
                new_state=STATE_APPROVED,
                extra={
                    "wp_url": None,
                    "wordpress_disabled": True,
                    "state": STATE_APPROVED,
                },
            )
            caption = claimed.get("caption") or ""
            return {
                "ok": True,
                "action": "approve",
                "state": STATE_APPROVED,
                "published": False,
                "duplicate": False,
                "wordpress_disabled": True,
                "url": None,
                "batch_id": batch_id,
                "event_id": event_id,
                "message_id": claimed.get("telegram_message_id"),
                "caption": caption,
                "telegram_caption_suffix": "✅ Approved\nWordPress publish is disabled for this test.",
                "reply_markup": empty_keyboard(),
            }
        url = result.get("url")
        store.cas_story_state(
            batch_id,
            event_id,
            expected=STATE_PUBLISHING,
            new_state=STATE_PUBLISHED,
            extra={
                "wp_url": url,
                "wp_post_id": result.get("post_id"),
                "state": STATE_PUBLISHED,
            },
        )
        caption = claimed.get("caption") or ""
        suffix = f"✅ Published\n{url}" if url else "✅ Published"
        return {
            "ok": True,
            "action": "approve",
            "state": STATE_PUBLISHED,
            "published": True,
            "duplicate": False,
            "url": url,
            "batch_id": batch_id,
            "event_id": event_id,
            "message_id": claimed.get("telegram_message_id"),
            "caption": caption,
            "telegram_caption_suffix": suffix,
            "reply_markup": empty_keyboard(),
        }
    except Exception as exc:
        raw = getattr(exc, "message", None) or str(exc)
        message = sanitize_wp_error(raw)
        store.cas_story_state(
            batch_id,
            event_id,
            expected=STATE_PUBLISHING,
            new_state=STATE_PUBLISH_FAILED,
            extra={"publish_error": message[:300]},
        )
        caption = claimed.get("caption") or ""
        return {
            "ok": False,
            "action": "approve",
            "state": STATE_PUBLISH_FAILED,
            "published": False,
            "reason": message[:300],
            "batch_id": batch_id,
            "event_id": event_id,
            "message_id": claimed.get("telegram_message_id"),
            "caption": caption,
            "telegram_caption_suffix": f"❌ Publish failed\n{message[:180]}",
            "reply_markup": empty_keyboard(),
        }
