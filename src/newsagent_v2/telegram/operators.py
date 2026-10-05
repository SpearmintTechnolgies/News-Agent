"""Send one operator message to every configured Telegram chat."""

from __future__ import annotations

from typing import Any

from newsagent_v2.telegram.config import TelegramConfig


def operator_ids(config: TelegramConfig) -> tuple[str, ...]:
    ids = getattr(config, "chat_ids", ())
    if isinstance(ids, (tuple, list)) and ids:
        return tuple(str(chat_id) for chat_id in ids)
    primary = str(getattr(config, "test_chat_id", "") or "").strip()
    return (primary,) if primary else ()


def broadcast_message(client: Any, config: TelegramConfig, **kwargs: Any) -> dict[str, Any]:
    """Send the same message to each operator. Returns the primary chat's result."""
    primary_id = str(getattr(config, "test_chat_id", "") or "")
    primary: dict[str, Any] = {"ok": False, "error": "no operator chat"}
    for chat_id in operator_ids(config):
        result = client.send_message(chat_id=chat_id, **kwargs)
        if chat_id == primary_id:
            primary = result
    return primary
