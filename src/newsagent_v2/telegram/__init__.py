"""V2 Telegram TEST delivery. Outbound only. No live send unless explicitly enabled later."""

from newsagent_v2.telegram.config import (
    TelegramConfig,
    TelegramConfigError,
    load_telegram_config,
    mask_token,
)
from newsagent_v2.telegram.batch import deliver_top5_batch
from newsagent_v2.telegram.contract import TelegramOutbound
from newsagent_v2.telegram.delivery import deliver_test_message

__all__ = [
    "TelegramConfig",
    "TelegramConfigError",
    "TelegramOutbound",
    "deliver_test_message",
    "deliver_top5_batch",
    "load_telegram_config",
    "mask_token",
]
