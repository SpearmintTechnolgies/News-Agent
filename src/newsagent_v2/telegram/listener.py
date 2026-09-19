"""Lightweight Telegram command listener. No live HTTP unless a transport is injected."""

from __future__ import annotations

from typing import Any, Callable

from newsagent_v2.approval.callbacks import handle_callback
from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.control.make import batch_busy, execute_make
from newsagent_v2.telegram.cards import html_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.contract import BUSY_MAKE_TEXT, SEND_TYPE_GET_UPDATES


def _apply_telegram_status_suffix(
    *,
    client: TelegramTestClient,
    chat_id: str,
    store: ApprovalStore,
    batch_id: str,
    event_id: str,
    result: dict[str, Any],
) -> None:
    """Append APPROVE/REJECT status. Never regenerates article body."""
    suffix = result.get("telegram_caption_suffix")
    if not suffix:
        return
    story = store.read_story(batch_id, event_id) or {}
    markup = result.get("reply_markup") or {"inline_keyboard": []}
    full_id = story.get("telegram_full_article_message_id")
    plain = story.get("telegram_full_article_plain")
    if full_id is not None and isinstance(plain, str) and plain.strip():
        client.edit_message_text(
            chat_id=chat_id,
            message_id=int(full_id),
            text=html_caption(f"{plain.rstrip()}\n\n{suffix}"),
            reply_markup=markup,
        )
        return
    # Image-card caption path.
    message_id = (
        story.get("telegram_image_message_id")
        or result.get("message_id")
        or story.get("telegram_message_id")
    )
    if message_id is None:
        return
    base = result.get("caption") or story.get("caption") or ""
    client.edit_message_caption(
        chat_id=chat_id,
        message_id=int(message_id),
        caption=html_caption(f"{base}\n\n{suffix}"),
        reply_markup=markup,
    )


def configured_chat_id(config: TelegramConfig) -> str:
    return str(config.test_chat_id).strip()


def update_chat_id(update: dict[str, Any]) -> str | None:
    message = update.get("message") or update.get("edited_message") or {}
    if isinstance(message, dict):
        chat = message.get("chat") or {}
        if isinstance(chat, dict) and chat.get("id") is not None:
            return str(chat.get("id"))
    callback = update.get("callback_query") or {}
    if isinstance(callback, dict):
        msg = callback.get("message") or {}
        chat = msg.get("chat") if isinstance(msg, dict) else {}
        if isinstance(chat, dict) and chat.get("id") is not None:
            return str(chat.get("id"))
    return None


def is_make_command(text: str | None) -> bool:
    if not isinstance(text, str):
        return False
    token = text.strip().split()[0] if text.strip() else ""
    if token == "/make":
        return True
    return token.startswith("/make@")


def handle_update(
    update: dict[str, Any],
    *,
    config: TelegramConfig,
    client: TelegramTestClient,
    store: ApprovalStore,
    pipeline: Callable[[], dict[str, Any]] | None = None,
    callback_handler: Callable[..., dict[str, Any]] | None = None,
    wp_publish_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    chat_id = update_chat_id(update)
    allowed = configured_chat_id(config)
    if chat_id is None or chat_id != allowed:
        return {"ok": False, "ignored": True, "reason": "wrong_chat"}

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        data = str(callback.get("data") or "")
        handler = callback_handler or handle_callback
        result = handler(data, store=store, publish_fn=wp_publish_fn)
        query_id = str(callback.get("id") or "")
        if query_id:
            client.answer_callback_query(callback_query_id=query_id, text=None)
        if result.get("telegram_caption_suffix"):
            _apply_telegram_status_suffix(
                client=client,
                chat_id=allowed,
                store=store,
                batch_id=str(result.get("batch_id") or ""),
                event_id=str(result.get("event_id") or ""),
                result=result,
            )
        result["ignored"] = False
        result["kind"] = "callback"
        return result

    message = update.get("message") or {}
    text = message.get("text") if isinstance(message, dict) else None
    if not is_make_command(text):
        return {"ok": True, "ignored": True, "reason": "not_make"}

    if batch_busy():
        client.send_message(chat_id=allowed, text=BUSY_MAKE_TEXT)
        return {"ok": False, "busy": True, "acked": False, "kind": "make"}

    if pipeline is None:
        raise ValueError("pipeline is required for /make")
    return execute_make(
        telegram_config=config,
        client=client,
        store=store,
        pipeline=pipeline,
    )


def poll_once(
    client: TelegramTestClient,
    *,
    offset: int | None = None,
    timeout: int = 0,
) -> dict[str, Any]:
    body: dict[str, Any] = {"timeout": int(timeout)}
    if offset is not None:
        body["offset"] = offset
    return client._post(SEND_TYPE_GET_UPDATES, json_body=body)


def run_listen_loop(
    *,
    config: TelegramConfig,
    client: TelegramTestClient,
    store: ApprovalStore,
    pipeline: Callable[[], dict[str, Any]],
    wp_publish_fn: Callable[..., dict[str, Any]] | None = None,
    poll_timeout: int = 30,
    max_iterations: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    offset: int | None = None
    iterations = 0
    while True:
        if should_stop is not None and should_stop():
            return
        if max_iterations is not None and iterations >= max_iterations:
            return
        iterations += 1
        response = poll_once(client, offset=offset, timeout=poll_timeout)
        payload = response.get("payload") if isinstance(response, dict) else None
        updates = []
        if isinstance(payload, dict) and isinstance(payload.get("result"), list):
            updates = payload["result"]
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            try:
                handle_update(
                    update,
                    config=config,
                    client=client,
                    store=store,
                    pipeline=pipeline,
                    wp_publish_fn=wp_publish_fn,
                )
            except Exception:
                continue
