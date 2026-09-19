"""
Live Telegram HTTP transport.

Used only when an explicit live-test opt-in is supplied.
Never logs or returns the token-bearing URL.
"""

from __future__ import annotations

import json as json_module
from typing import Any

import requests

from newsagent_v2.telegram.client import API_BASE, DEFAULT_TIMEOUT_SECONDS, Transport
from newsagent_v2.telegram.config import TelegramConfig


class LiveTelegramResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None, headers: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, Any] | None:
        return self._payload


def build_live_transport(config: TelegramConfig) -> Transport:
    token = config.bot_token

    def transport(
        _redacted_url: str,
        *,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        method: str,
        **_kwargs: Any,
    ) -> LiveTelegramResponse:
        url = f"{API_BASE}/bot{token}/{method}"
        try:
            if files:
                form = dict(json or {})
                markup = form.get("reply_markup")
                if isinstance(markup, (dict, list)):
                    form["reply_markup"] = json_module.dumps(markup)
                response = requests.post(url, data=form, files=files, timeout=timeout)
            else:
                response = requests.post(url, json=json, timeout=timeout)
        except requests.RequestException:
            return LiveTelegramResponse(
                status_code=0,
                payload={
                    "ok": False,
                    "error_code": None,
                    "description": "Telegram HTTP request failed",
                },
                headers={},
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {"ok": False, "description": "provider JSON was not an object"}
        except ValueError:
            payload = {"ok": False, "description": "provider response was not JSON"}
        return LiveTelegramResponse(response.status_code, payload, response.headers)

    return transport
