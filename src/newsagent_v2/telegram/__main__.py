"""V2 Telegram TEST CLI. Live send requires explicit --live-test."""

from __future__ import annotations

import argparse
import json
import os
import sys

from newsagent_v2.telegram.config import (
    CHAT_ENV,
    TOKEN_ENV,
    TelegramConfigError,
    load_telegram_config,
)
from newsagent_v2.telegram.contract import LIVE_SEND_ENABLED, TelegramOutbound
from newsagent_v2.telegram.delivery import deliver_test_message

CONNECTIVITY_EVENT_ID = "telegram-connectivity-test"

LIVE_TEST_PREREQUISITES = """
NewsAgent V2 Telegram live TEST send requires an explicit opt-in.

Without --live-test: NO NETWORK SEND.

Required env vars (do not paste values into chat):
  NEWSAGENT_V2_TELEGRAM_BOT_TOKEN
  NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID
"""


def connectivity_payload() -> TelegramOutbound:
    return TelegramOutbound(
        event_id=CONNECTIVITY_EVENT_ID,
        headline="Telegram delivery connected successfully.",
        dek="Mode: TEST\nPurpose: V2 connectivity verification",
        mode="test",
    )


def _missing_env_names(environ: dict[str, str]) -> list[str]:
    missing: list[str] = []
    if not str(environ.get(TOKEN_ENV, "")).strip():
        missing.append(TOKEN_ENV)
    if not str(environ.get(CHAT_ENV, "")).strip():
        missing.append(CHAT_ENV)
    return missing


def _public_result(result: dict) -> dict:
    send = result.get("send_result") or {}
    telemetry = result.get("telemetry") or {}
    return {
        "run_id": result.get("run_id"),
        "success": result.get("success"),
        "http_status": send.get("http_status") or telemetry.get("http_status"),
        "message_id": send.get("message_id") or telemetry.get("message_id"),
        "retry_count": send.get("retry_count"),
        "latency_ms": send.get("latency_ms") or telemetry.get("latency_ms"),
        "send_type": telemetry.get("send_type"),
        "image_attached": send.get("image_attached"),
        "mock": result.get("mock"),
        "offline": result.get("offline"),
        "live_test": result.get("live_test"),
        "failure_reason": send.get("failure_reason"),
        "artifact_paths": result.get("artifact_paths"),
        "default_live_send_enabled": LIVE_SEND_ENABLED,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--live-test",
        action="store_true",
        help="Explicit opt-in for ONE V2 Telegram TEST send. Default remains off.",
    )
    args = parser.parse_args(argv)

    if not args.live_test:
        print(LIVE_TEST_PREREQUISITES.strip())
        print("Refusing live Telegram send.")
        print(f"LIVE_SEND_ENABLED default={LIVE_SEND_ENABLED}")
        return 2

    environ = {key: str(value) for key, value in os.environ.items()}
    missing = _missing_env_names(environ)
    if missing:
        print("STOP. Missing environment variable(s): " + ", ".join(missing))
        print("Do not paste secret values into chat.")
        return 2

    try:
        config = load_telegram_config(
            {
                TOKEN_ENV: environ[TOKEN_ENV],
                CHAT_ENV: environ[CHAT_ENV],
            }
        )
    except TelegramConfigError as exc:
        print(f"STOP. Config error (names only): {exc}")
        return 2

    result = deliver_test_message(
        connectivity_payload(),
        config,
        live_test=True,
        persist=True,
    )
    print(json.dumps(_public_result(result), ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
