"""
Strict Telegram TEST config.

Reads ONLY:
  NEWSAGENT_V2_TELEGRAM_BOT_TOKEN
  NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID

Never falls back to another bot, chat, or environment name.
"""

from __future__ import annotations

from dataclasses import dataclass

TOKEN_ENV = "NEWSAGENT_V2_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID"


class TelegramConfigError(ValueError):
    """Missing or invalid V2 Telegram TEST configuration."""


def parse_chat_ids(raw: str) -> tuple[str, ...]:
    """Comma-separated operator chats. The first is the primary chat."""
    ids: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        chat = part.strip()
        if not chat:
            continue
        if not chat.lstrip("-").isdigit():
            raise TelegramConfigError(f"{CHAT_ENV} has a non-numeric chat id: {chat}")
        if chat not in ids:
            ids.append(chat)
    return tuple(ids)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    test_chat_id: str
    test_mode: bool = True
    chat_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        token = self.bot_token.strip()
        ids = parse_chat_ids(self.test_chat_id)
        if not token:
            raise TelegramConfigError(f"{TOKEN_ENV} is empty")
        if not ids:
            raise TelegramConfigError(f"{CHAT_ENV} is empty")
        if not self.test_mode:
            raise TelegramConfigError("Telegram delivery requires test_mode=True")
        object.__setattr__(self, "bot_token", token)
        object.__setattr__(self, "test_chat_id", ids[0])
        object.__setattr__(self, "chat_ids", ids)

    def allows(self, chat_id: str | None) -> bool:
        return str(chat_id or "").strip() in self.chat_ids


def mask_token(token: str | None) -> str:
    if not token:
        return "[REDACTED]"
    cleaned = str(token).strip()
    if len(cleaned) < 10:
        return "[REDACTED]"
    return f"{cleaned[:3]}…{cleaned[-2:]}"


def load_telegram_config(environ: dict[str, str] | None) -> TelegramConfig:
    if environ is None:
        raise TelegramConfigError(
            "environ must be provided explicitly; process-wide fallback is not used"
        )
    raw_token = environ.get(TOKEN_ENV)
    raw_chat = environ.get(CHAT_ENV)
    if raw_token is None:
        raise TelegramConfigError(f"{TOKEN_ENV} is not set")
    if raw_chat is None:
        raise TelegramConfigError(f"{CHAT_ENV} is not set")
    return TelegramConfig(
        bot_token=str(raw_token),
        test_chat_id=str(raw_chat),
        test_mode=True,
    )
