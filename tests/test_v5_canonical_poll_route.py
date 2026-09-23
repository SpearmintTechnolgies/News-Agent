"""Mocked tests for Canonical V5 Telegram poll/route /make path.

No live Telegram, research, WordPress, or Kimi calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from newsagent_v2.telegram.contract import ACK_MAKE_TEXT
from newsagent_v2.telegram.v5_canonical_runtime import CanonicalV5Integration


CHAT_ID = "-5238995962"


def _runtime(*, chat_id: str = CHAT_ID) -> CanonicalV5Integration:
    runtime = CanonicalV5Integration.__new__(CanonicalV5Integration)
    runtime.config = SimpleNamespace(test_chat_id=chat_id)
    runtime.client = MagicMock()
    runtime.client.timeout_seconds = 30
    runtime.client.send_message.side_effect = lambda **kwargs: {"ok": True, **kwargs}
    runtime.client.answer_callback_query.side_effect = lambda **kwargs: {"ok": True}
    runtime._poll_offset = None
    runtime._try_capture_feedback = MagicMock(return_value=None)
    runtime.handle_callback = MagicMock(return_value={"ok": True})
    runtime.run_discovery = MagicMock(return_value=[])
    return runtime


def _ok_updates(updates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "status_code": 200,
        "payload": {"ok": True, "result": updates},
    }


def _make_update(
    *,
    update_id: int,
    text: str,
    chat_id: str = CHAT_ID,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "text": text,
            "chat": {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id},
        },
    }


def test_make_at_bot_routes_and_acks_before_discovery():
    """Group clients often send /make@BotName; exact '== /make' silently dropped those."""
    runtime = _runtime()
    update = _make_update(update_id=101, text="/make@NewsAgentV2Bot")

    with patch(
        "newsagent_v2.telegram.v5_canonical_runtime.poll_once",
        return_value=_ok_updates([update]),
    ) as poll:
        result = runtime.run_once()

    poll.assert_called_once()
    kwargs = poll.call_args.kwargs
    assert kwargs["offset"] is None
    # HTTP timeout 30 -> long-poll must be lower so empty polls do not read-timeout.
    assert kwargs["timeout"] == 25

    assert result["handled"] == 1
    assert result["updates"] == 1
    assert runtime._poll_offset == 102
    runtime.run_discovery.assert_called_once()

    sent_texts = [c.kwargs.get("text") for c in runtime.client.send_message.call_args_list]
    assert sent_texts[0] == ACK_MAKE_TEXT
    assert runtime.client.send_message.call_args_list[0].kwargs["chat_id"] == CHAT_ID


def test_plain_make_still_routes():
    runtime = _runtime()
    update = _make_update(update_id=7, text="/make")

    with patch(
        "newsagent_v2.telegram.v5_canonical_runtime.poll_once",
        return_value=_ok_updates([update]),
    ):
        result = runtime.run_once()

    assert result["handled"] == 1
    runtime.run_discovery.assert_called_once()
    assert runtime.client.send_message.call_args_list[0].kwargs["text"] == ACK_MAKE_TEXT


def test_wrong_chat_is_skipped_without_make(capsys):
    runtime = _runtime(chat_id=CHAT_ID)
    update = _make_update(update_id=9, text="/make", chat_id="-111")

    with patch(
        "newsagent_v2.telegram.v5_canonical_runtime.poll_once",
        return_value=_ok_updates([update]),
    ):
        result = runtime.run_once()

    assert result["handled"] == 0
    runtime.run_discovery.assert_not_called()
    runtime.client.send_message.assert_not_called()
    # Offset still advances so updates are not swallowed/replayed forever.
    assert runtime._poll_offset == 10
    err = capsys.readouterr().out
    assert "[ROUTE] skip" in err


def test_poll_failure_is_logged_not_silent(capsys):
    runtime = _runtime()

    with patch(
        "newsagent_v2.telegram.v5_canonical_runtime.poll_once",
        return_value={
            "ok": False,
            "status_code": 408,
            "error": "timeout",
            "payload": {"ok": False, "error": "timeout"},
        },
    ):
        result = runtime.run_once()

    assert result["handled"] == 0
    assert result.get("poll_error")
    assert "[POLL] getUpdates failed" in capsys.readouterr().out
    runtime.run_discovery.assert_not_called()


def test_make_handler_exception_is_logged_and_notified(capsys):
    runtime = _runtime()
    runtime.run_discovery.side_effect = RuntimeError("boom-discovery")
    update = _make_update(update_id=3, text="/make")

    with patch(
        "newsagent_v2.telegram.v5_canonical_runtime.poll_once",
        return_value=_ok_updates([update]),
    ):
        result = runtime.run_once()

    assert result["handled"] == 1
    out = capsys.readouterr().out
    assert "[ROUTE] /make handler error" in out
    texts = [c.kwargs.get("text") for c in runtime.client.send_message.call_args_list]
    assert texts[0] == ACK_MAKE_TEXT
    assert any("boom-discovery" in str(t) for t in texts[1:])


def test_old_exact_make_equality_would_miss_at_form():
    """Document the pre-fix bug: exact equality rejects /make@Bot."""
    token = "/make@NewsAgentV2Bot".strip().split()[0]
    assert token != "/make"
    from newsagent_v2.telegram.listener import is_make_command

    assert is_make_command("/make@NewsAgentV2Bot") is True
