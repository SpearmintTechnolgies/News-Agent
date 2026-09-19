"""
Thin Telegram Bot API client for V2 TEST delivery.

Live network is disabled. Inject a transport for mocked tests.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from newsagent_v2.telegram.config import TelegramConfig, mask_token
from newsagent_v2.telegram.contract import (
    LIVE_SEND_ENABLED,
    SEND_TYPE_ANSWER_CALLBACK,
    SEND_TYPE_EDIT_CAPTION,
    SEND_TYPE_EDIT_TEXT,
    SEND_TYPE_MESSAGE,
    SEND_TYPE_PHOTO,
)

API_BASE = "https://api.telegram.org"
TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 15
DEFAULT_TIMEOUT_SECONDS = 30

Transport = Callable[..., Any]


class TelegramLiveDisabledError(RuntimeError):
    """Raised when a live Telegram HTTP call is attempted."""


class TelegramHttpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def redacted_api_url(method: str) -> str:
    return f"{API_BASE}/bot[REDACTED]/{method}"


def _retry_after_seconds(headers: Any) -> float:
    if headers is None:
        return 1.0
    raw = None
    if hasattr(headers, "get"):
        raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return 1.0
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if wait < 0:
        return 0.0
    return min(wait, float(MAX_RETRY_AFTER_SECONDS))


def _status_code(response: Any) -> int:
    return int(getattr(response, "status_code"))


def _response_json(response: Any) -> dict[str, Any] | None:
    if not hasattr(response, "json"):
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def refuse_live_transport(*args: Any, **kwargs: Any) -> Any:
    raise TelegramLiveDisabledError(
        "Live Telegram HTTP is disabled. Inject a mock transport. "
        f"token_fingerprint={mask_token(None)}"
    )


class TelegramTestClient:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        live_send_enabled: bool = LIVE_SEND_ENABLED,
    ) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.live_send_enabled = live_send_enabled
        self._sleep = sleep or (lambda _seconds: None)
        if transport is None:
            if not live_send_enabled:
                self._transport = refuse_live_transport
            else:
                raise TelegramLiveDisabledError(
                    "Live Telegram transport is not wired in this task"
                )
        else:
            self._transport = transport

    def public_request_metadata(
        self,
        method: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": redacted_api_url(method),
            "timeout_seconds": self.timeout_seconds,
            "body": body,
            "headers_included": False,
            "token_included": False,
            "token_fingerprint": mask_token(self.config.bot_token),
            "mock": not self.live_send_enabled,
            "offline": not self.live_send_enabled,
        }

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return self._post(SEND_TYPE_MESSAGE, json_body=body)

    def send_photo(
        self,
        *,
        chat_id: str,
        photo_name: str,
        photo_bytes: bytes,
        caption: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        files = {"photo": (photo_name, photo_bytes)}
        return self._post(SEND_TYPE_PHOTO, json_body=body, files=files)

    def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        return self._post(SEND_TYPE_ANSWER_CALLBACK, json_body=body)

    def edit_message_caption(
        self,
        *,
        chat_id: str,
        message_id: int,
        caption: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return self._post(SEND_TYPE_EDIT_CAPTION, json_body=body)

    def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return self._post(SEND_TYPE_EDIT_TEXT, json_body=body)

    def _post(
        self,
        api_method: str,
        *,
        json_body: dict[str, Any],
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = 0
        last_status: int | None = None
        last_payload: dict[str, Any] | None = None
        last_error: str | None = None

        while attempts < 2:
            attempts += 1
            response = self._transport(
                redacted_api_url(api_method),
                json=json_body,
                files=files,
                timeout=self.timeout_seconds,
                method=api_method,
            )
            last_status = _status_code(response)
            last_payload = _response_json(response)
            retry_count = attempts - 1

            if last_status == 200 and isinstance(last_payload, dict) and last_payload.get("ok"):
                result = last_payload.get("result") if isinstance(last_payload.get("result"), dict) else {}
                message_id = result.get("message_id") if isinstance(result, dict) else None
                return {
                    "ok": True,
                    "status_code": last_status,
                    "payload": last_payload,
                    "retry_count": retry_count,
                    "message_id": int(message_id) if isinstance(message_id, int) else None,
                    "telegram_error_code": None,
                    "telegram_description": None,
                }

            error_code = None
            description = None
            if isinstance(last_payload, dict):
                if isinstance(last_payload.get("error_code"), int):
                    error_code = last_payload["error_code"]
                if isinstance(last_payload.get("description"), str):
                    description = last_payload["description"]
            last_error = description or f"HTTP {last_status}"

            if last_status in TRANSIENT_STATUS_CODES and attempts < 2:
                headers = getattr(response, "headers", {}) or {}
                self._sleep(_retry_after_seconds(headers))
                continue

            return {
                "ok": False,
                "status_code": last_status,
                "payload": last_payload,
                "retry_count": retry_count,
                "message_id": None,
                "telegram_error_code": error_code,
                "telegram_description": description,
                "error": last_error,
            }

        return {
            "ok": False,
            "status_code": last_status,
            "payload": last_payload,
            "retry_count": 1,
            "message_id": None,
            "telegram_error_code": None,
            "telegram_description": last_error,
            "error": last_error,
        }


def assert_url_has_no_token(url: str, token: str) -> None:
    parsed = urlparse(url)
    if token and token in url:
        raise TelegramHttpError("request URL must not include the bot token")
    if "bot" in parsed.path and "[REDACTED]" not in parsed.path:
        raise TelegramHttpError("request URL is not redacted")
