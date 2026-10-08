"""Route operator messages to the Telegram chat that started the action.

Several chats may operate the bot (NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID is a comma
list; the first id is the primary chat). While an update from an allowed chat is
being handled, ``set_origin_chat`` records that chat, and every operator send
(cards, progress, results) goes back to it. Background work started from that
update inherits the origin via ``contextvars``. Sends with no originating chat
(startup menu, resumed jobs) go to the primary chat only.
"""

from __future__ import annotations

import contextvars
from typing import Any

from newsagent_v2.telegram.config import TelegramConfig

_ORIGIN_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "newsagent_origin_chat", default=""
)


def set_origin_chat(chat_id: Any) -> contextvars.Token:
    """Remember which chat the current action came from ("" clears it)."""
    return _ORIGIN_CHAT.set(str(chat_id or "").strip())


def origin_chat() -> str:
    return _ORIGIN_CHAT.get()


def all_operator_ids(config: TelegramConfig) -> tuple[str, ...]:
    ids = getattr(config, "chat_ids", ())
    if isinstance(ids, (tuple, list)) and ids:
        return tuple(str(chat_id) for chat_id in ids)
    primary = str(getattr(config, "test_chat_id", "") or "").strip()
    return (primary,) if primary else ()


def operator_ids(config: TelegramConfig) -> tuple[str, ...]:
    """The chat to send to: the originating chat if allowed, else the primary chat."""
    ids = all_operator_ids(config)
    origin = _ORIGIN_CHAT.get()
    if origin and origin in ids:
        return (origin,)
    return ids[:1]


def broadcast_message(client: Any, config: TelegramConfig, **kwargs: Any) -> dict[str, Any]:
    """Send an operator message (see ``operator_ids``). Returns the first send result."""
    first: dict[str, Any] | None = None
    for chat_id in operator_ids(config):
        result = client.send_message(chat_id=chat_id, **kwargs)
        if first is None:
            first = result
    return first or {"ok": False, "error": "no operator chat"}
