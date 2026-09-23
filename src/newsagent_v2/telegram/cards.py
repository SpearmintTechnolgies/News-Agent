"""Approval-card captions and callback payloads."""

from __future__ import annotations

import html
import re

from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.telegram.contract import CALLBACK_DATA_LIMIT

APPROVE_PREFIX = "ap"
REJECT_PREFIX = "rj"


def short_summary(dek: str | None, *, max_sentences: int = 2) -> str:
    text = " ".join((dek or "").split())
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:max_sentences]).strip()


def approval_caption(
    *,
    rank: int,
    headline: str,
    dek: str | None,
    total: int | None = None,
    article_type: str | None = None,
    body_words: int | None = None,
    kimi_usage: dict | None = None,
    wordpress_draft: dict | None = None,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    seo_status: str | None = None,
) -> str:
    count = MAKE_STORY_COUNT if total is None else int(total)
    summary = short_summary(dek)
    lines = [f"📰 {rank}/{count}", "", headline.strip()]
    if summary:
        lines.extend(["", summary])
    if article_type:
        lines.extend(["", f"Type: {article_type}"])
    if body_words is not None:
        if article_type:
            lines.append(f"Words: {body_words}")
        else:
            lines.extend(["", f"Words: {body_words}"])
    if kimi_usage:
        def _usage(name: str) -> str:
            value = kimi_usage.get(name)
            return str(value) if value is not None else "n/a"

        lines.extend(
            [
                "",
                f"Kimi: {_usage('request_count')} requests",
                f"Tokens: {_usage('prompt_tokens')} prompt / {_usage('completion_tokens')} completion / {_usage('total_tokens')} total",
                f"Kimi status: {_usage('status')}",
                f"Kimi cost: {_usage('cost_display')}",
            ]
        )
    if wordpress_draft:
        lines.extend(
            [
                "",
                f"WP draft: {wordpress_draft.get('wp_url') or 'n/a'} ({wordpress_draft.get('status') or 'n/a'})",
            ]
        )
    if categories is not None:
        lines.append(f"Categories: {', '.join(categories) or 'none'}")
    if tags is not None:
        lines.append(f"Tags: {', '.join(tags) or 'none'}")
    if seo_status:
        lines.append(f"SEO: {seo_status}")
    return "\n".join(lines).strip()


def html_caption(text: str) -> str:
    return html.escape(text, quote=True)


def callback_data(action: str, batch_id: str, event_id: str) -> str:
    if action not in {APPROVE_PREFIX, REJECT_PREFIX}:
        raise ValueError("callback action must be ap or rj")
    payload = f"{action}:{batch_id}:{event_id}"
    if len(payload) > CALLBACK_DATA_LIMIT:
        raise ValueError("callback data exceeds Telegram 64-byte limit")
    return payload


def parse_callback_data(raw: str) -> dict[str, str] | None:
    text = (raw or "").strip()
    parts = text.split(":")
    if len(parts) != 3:
        return None
    action, batch_id, event_id = parts
    if action not in {APPROVE_PREFIX, REJECT_PREFIX}:
        return None
    if not batch_id or not event_id:
        return None
    return {"action": action, "batch_id": batch_id, "event_id": event_id}


def approval_keyboard(batch_id: str, event_id: str) -> dict[str, list]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ APPROVE",
                    "callback_data": callback_data(APPROVE_PREFIX, batch_id, event_id),
                },
                {
                    "text": "❌ REJECT",
                    "callback_data": callback_data(REJECT_PREFIX, batch_id, event_id),
                },
            ]
        ]
    }


def empty_keyboard() -> dict[str, list]:
    return {"inline_keyboard": []}
