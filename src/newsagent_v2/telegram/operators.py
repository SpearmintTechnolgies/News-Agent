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

from newsagent_v2.telegram import access
from newsagent_v2.telegram.config import TelegramConfig

_ORIGIN_CHAT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "newsagent_origin_chat", default=""
)


_ACTOR_ID: contextvars.ContextVar[str] = contextvars.ContextVar("newsagent_actor_id", default="")


def set_actor_id(actor_id: Any) -> contextvars.Token:
    return _ACTOR_ID.set(str(actor_id or ""))


def actor_id() -> str:
    return _ACTOR_ID.get()


def actor_from_update(update: dict) -> str:
    """The numeric Telegram user behind a message or button press ('' for bots and channel posts)."""
    source = update.get("callback_query") or update.get("message") or {}
    actor = source.get("from") or {}
    raw = str(actor.get("id") or "")
    if source.get("sender_chat") or actor.get("is_bot") or not raw.isdigit() or int(raw) <= 0:
        return ""
    return str(int(raw))


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
    if origin and (origin in ids or access.allows(origin, actor_id())):
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
