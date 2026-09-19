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


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    test_chat_id: str
    test_mode: bool = True

    def __post_init__(self) -> None:
        token = self.bot_token.strip()
        chat = self.test_chat_id.strip()
        if not token:
            raise TelegramConfigError(f"{TOKEN_ENV} is empty")
        if not chat:
            raise TelegramConfigError(f"{CHAT_ENV} is empty")
        if not self.test_mode:
            raise TelegramConfigError("Telegram delivery requires test_mode=True")
        object.__setattr__(self, "bot_token", token)
        object.__setattr__(self, "test_chat_id", chat)


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
