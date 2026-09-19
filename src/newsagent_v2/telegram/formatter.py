"""Deterministic Telegram HTML formatter. Does not rewrite article facts."""

from __future__ import annotations

import html
import re

from newsagent_v2.telegram.contract import (
    KIND_BATCH_HEADER,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    TelegramOutbound,
)

ELLIPSIS = "…"
WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\")


def escape_html(text: str) -> str:
    return html.escape(text, quote=True)


def _optional_line(label: str, value: str | None) -> str:
    if value is None or not str(value).strip():
        return ""
    return f"{label}: {escape_html(str(value).strip())}"


def format_outbound_text(
    payload: TelegramOutbound,
    *,
    caption: bool = False,
) -> dict[str, object]:
    headline = (payload.headline or "").strip()
    dek = (payload.dek or "").strip()
    category = (payload.category or "").strip() or None
    limit = TELEGRAM_CAPTION_LIMIT if caption else TELEGRAM_MESSAGE_LIMIT

    if payload.kind == KIND_BATCH_HEADER:
        text = escape_html(headline or "[TEST] CoinNetwork V2 — Top 5")
        truncated = False
        if len(text) > limit:
            truncated = True
            text = text[: max(0, limit - len(ELLIPSIS))].rstrip() + ELLIPSIS
        return {
            "text": text,
            "parse_mode": "HTML",
            "length": len(text),
            "truncated": truncated,
            "limit": limit,
        }

    parts = [
        escape_html("[TEST] CoinNetwork V2 — Top 5")
        if payload.story_index is not None
        else escape_html("[TEST] CoinNetwork V2"),
    ]
    if payload.story_index is not None:
        count = payload.story_count or payload.story_index
        parts.extend(["", escape_html(f"Story {payload.story_index}/{count}")])
    parts.extend(["", f"<strong>{escape_html(headline)}</strong>"])
    if dek:
        parts.extend(["", escape_html(dek)])

    meta_lines = []
    if category:
        meta_lines.append(_optional_line("Category", category))
    if payload.source_count is not None:
        meta_lines.append(_optional_line("Sources", str(payload.source_count)))
    if payload.cost_summary:
        meta_lines.append(_optional_line("Cost", payload.cost_summary.strip()))
    if meta_lines:
        parts.extend(["", *meta_lines])

    if payload.article_url:
        href = escape_html(payload.article_url.strip())
        parts.extend(["", f'Article: <a href="{href}">Open draft</a>'])

    text = "\n".join(parts).strip()
    truncated = False
    if len(text) > limit:
        truncated = True
        text = text[: max(0, limit - len(ELLIPSIS))].rstrip() + ELLIPSIS

    return {
        "text": text,
        "parse_mode": "HTML",
        "length": len(text),
        "truncated": truncated,
        "limit": limit,
    }
