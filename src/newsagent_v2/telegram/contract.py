"""Deterministic Telegram TEST outbound contract. Delivery only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_MODE_TEST = "test"
SEND_TYPE_MESSAGE = "sendMessage"
SEND_TYPE_PHOTO = "sendPhoto"
SEND_TYPE_ANSWER_CALLBACK = "answerCallbackQuery"
SEND_TYPE_EDIT_CAPTION = "editMessageCaption"
SEND_TYPE_EDIT_TEXT = "editMessageText"
SEND_TYPE_GET_UPDATES = "getUpdates"
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
KIND_STORY = "story"
KIND_BATCH_HEADER = "batch_header"
CALLBACK_DATA_LIMIT = 64
ACK_MAKE_TEXT = "🔄 NewsAgent is preparing the latest 5 stories..."
BUSY_MAKE_TEXT = "⚠️ NewsAgent batch already running."

LIVE_SEND_ENABLED = False


@dataclass(frozen=True)
class TelegramOutbound:
    event_id: str
    headline: str
    category: str | None = None
    dek: str | None = None
    article_url: str | None = None
    image_path: str | None = None
    source_count: int | None = None
    cost_summary: str | None = None
    mode: str = TELEGRAM_MODE_TEST
    chat_id: str | None = None
    kind: str = KIND_STORY
    story_index: int | None = None
    story_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TelegramSendResult:
    success: bool
    message_id: int | None = None
    chat_id: str | None = None
    http_status: int | None = None
    retry_count: int = 0
    latency_ms: int | None = None
    failure_reason: str | None = None
    telegram_error_code: int | None = None
    telegram_description: str | None = None
    send_type: str | None = None
    caption_length: int | None = None
    image_attached: bool = False
    http_called: bool = False
    mock: bool = True
    offline: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "http_status": self.http_status,
            "retry_count": self.retry_count,
            "latency_ms": self.latency_ms,
            "failure_reason": self.failure_reason,
            "telegram_error_code": self.telegram_error_code,
            "telegram_description": self.telegram_description,
            "send_type": self.send_type,
            "caption_length": self.caption_length,
            "image_attached": self.image_attached,
            "http_called": self.http_called,
            "mock": self.mock,
            "offline": self.offline,
        }
