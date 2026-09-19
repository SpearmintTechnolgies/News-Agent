"""Telegram TEST telemetry. Never persists bot tokens or Authorization headers."""

from __future__ import annotations

from typing import Any

from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.telegram.config import mask_token


def build_telegram_telemetry(
    *,
    run_id: str,
    event_id: str | None,
    telegram_mode: str,
    send_type: str | None,
    chat_id: str | None,
    message_id: int | None,
    success: bool,
    http_status: int | None,
    retry_count: int,
    latency_ms: int | None,
    failure_reason: str | None,
    telegram_error_code: int | None,
    telegram_description: str | None,
    caption_length: int | None,
    image_attached: bool,
    timestamp_utc: str,
    mock: bool,
    offline: bool,
    secrets: list[str] | tuple[str, ...] = (),
    token_fingerprint: str | None = None,
) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "event_id": event_id,
        "telegram_mode": telegram_mode,
        "send_type": send_type,
        "chat_id": chat_id,
        "message_id": message_id,
        "success": success,
        "http_status": http_status,
        "retry_count": retry_count,
        "latency_ms": latency_ms,
        "failure_reason": failure_reason,
        "telegram_error_code": telegram_error_code,
        "telegram_description": telegram_description,
        "caption_length": caption_length,
        "image_attached": image_attached,
        "timestamp_utc": timestamp_utc,
        "mock": mock,
        "offline": offline,
        "token_fingerprint": token_fingerprint or mask_token(None),
        "headers_included": False,
    }
    redacted = redact_secrets(payload, secrets)
    return redact_telegram_keys(redacted, secrets)


def redact_telegram_keys(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    blocked = {s for s in secrets if s}
    return _redact(value, blocked)


def _redact(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {
                "authorization",
                "bot_token",
                "token",
                "newsagent_v2_telegram_bot_token",
            }:
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact(item, secrets)
        return out
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret and secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text
    return value
