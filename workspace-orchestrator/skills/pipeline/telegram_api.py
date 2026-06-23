#!/usr/bin/env python3
"""telegram_api.py — Proxy-aware Telegram Bot API helpers for pipeline scripts.

Reads the same proxy URL OpenClaw uses:
  openclaw.json → channels.telegram.accounts.<account>.proxy

Override for debugging: TELEGRAM_PROXY_URL env var.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
TELEGRAM_CONFIG = os.path.expanduser(
    "~/.openclaw/workspace-orchestrator/config/telegram_card_config.json"
)

_UNSET = object()
_PROXY_CACHE: str | None | object = _UNSET


def _load_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_proxy_url(account_id: str | None = None) -> str | None:
    """Return HTTP(S) proxy URL for Telegram Bot API, or None for direct."""
    global _PROXY_CACHE
    if _PROXY_CACHE is not _UNSET:
        return _PROXY_CACHE  # type: ignore[return-value]

    env_override = os.environ.get("TELEGRAM_PROXY_URL", "").strip()
    if env_override:
        _PROXY_CACHE = env_override
        return env_override

    tg_cfg = _load_json(TELEGRAM_CONFIG)
    acct = (account_id or str(tg_cfg.get("telegram_account") or "news")).strip() or "news"

    openclaw = _load_json(OPENCLAW_JSON)
    telegram = (openclaw.get("channels") or {}).get("telegram") or {}
    account = (telegram.get("accounts") or {}).get(acct) or {}
    proxy = str(account.get("proxy") or "").strip()
    _PROXY_CACHE = proxy or None
    return _PROXY_CACHE


def _build_opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    if proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def urlopen(req: urllib.request.Request, *, timeout: int = 60) -> Any:
    """Open a URL via the configured Telegram proxy (if any)."""
    opener = _build_opener(load_proxy_url())
    return opener.open(req, timeout=timeout)


def telegram_request(
    token: str,
    method: str,
    data: dict | None = None,
    files: dict | None = None,
    *,
    _allow_retry: bool = True,
) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = "----OpenClawBoundary"
        body_parts: list[bytes] = []
        for name, (filename, content, mime) in files.items():
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            body_parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
            body_parts.append(content)
            body_parts.append(b"\r\n")
        if data:
            for key, val in data.items():
                body_parts.append(f"--{boundary}\r\n".encode())
                body_parts.append(
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                )
                body_parts.append(str(val).encode("utf-8"))
                body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
    else:
        encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
    result = json.loads(raw)
    if not result.get("ok"):
        if _allow_retry and result.get("error_code") == 429:
            params = result.get("parameters") or {}
            retry_after = max(int(params.get("retry_after", 1)), 1)
            time.sleep(retry_after)
            return telegram_request(token, method, data, files, _allow_retry=False)
        desc = result.get("description", "unknown Telegram error")
        raise RuntimeError(desc)
    return result


def download_telegram_file(token: str, file_id: str) -> bytes:
    result = telegram_request(token, "getFile", {"file_id": file_id})
    file_path = (result.get("result") or {}).get("file_path")
    if not file_path:
        raise RuntimeError("getFile returned no path")
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    req = urllib.request.Request(url, method="GET")
    with urlopen(req, timeout=60) as resp:
        return resp.read()
