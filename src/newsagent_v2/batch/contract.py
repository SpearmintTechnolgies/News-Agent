"""Top-5 batch contract. Reuses existing ranking, QA, image, and TEST Telegram."""

from __future__ import annotations

TOP5_COUNT = 5
# Temporary /make vertical slice: one viable story end-to-end.
MAKE_STORY_COUNT = 1
BATCH_SCHEMA_VERSION = "top5-batch-v1"
BATCH_HEADER_EVENT_ID = "top5-batch-header"
BATCH_HEADER_TEXT = "[TEST] CoinNetwork V2 — Top 5"


class BatchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
