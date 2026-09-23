"""Publish frozen CoinNetwork articles. Does not regenerate copy or images."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

from newsagent_v2.wordpress.config import WordPressConfig

Transport = Callable[..., Any]
SECRET_SHAPE_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")

# Explicit product UA for WordPress REST. Do not spoof another application.
NEWSAGENT_WP_USER_AGENT = "NewsAgent-V2/1.0 (WordPress REST; +https://coinnetwork.info)"

_SG_CAPTCHA_MARKERS = (
    "sg-captcha",
    "sgcaptcha",
    "/.well-known/sgcaptcha/",
    "sgcaptcha/challenge",
)


class WordPressPublishError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sanitize_wp_error(text: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = str(text or "publish failed")
    cleaned = SECRET_SHAPE_RE.sub("[REDACTED]", cleaned)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    for needle in ("Authorization", "application password", "app_password"):
        if needle.lower() in cleaned.lower() and "REDACTED" not in cleaned:
            cleaned = "publish failed"
    return cleaned[:300]


def is_siteground_captcha_response(
    *,
    status_code: int,
    headers: Mapping[str, Any] | None,
    body_text: str,
) -> bool:
    """True when the response is a SiteGround sg-captcha challenge, not WP JSON."""
    text = (body_text or "").lower()
    header_blob = ""
    if headers:
        try:
            items = list(headers.items())
        except Exception:
            items = []
        header_blob = " ".join(f"{k}:{v}" for k, v in items).lower()

    if any(marker in header_blob for marker in _SG_CAPTCHA_MARKERS):
        return True
    if any(marker in text for marker in _SG_CAPTCHA_MARKERS):
        return True
    # SiteGround often answers the challenge with HTTP 202 + HTML.
    if status_code == 202 and (
        "<html" in text or "<!doctype" in text or "text/html" in header_blob
    ):
        return True
    return False


def publish_frozen_story(
    *,
    config: WordPressConfig,
    article: dict[str, Any],
    image_path: str | None,
    transport: Transport,
    status: str = "draft",
) -> dict[str, Any]:
    """POST media (optional) then posts. Artifacts are used as-is."""
    secrets = config.secrets()
    media_id = None
    if image_path:
        path = Path(image_path)
        if not path.is_file():
            raise WordPressPublishError("image_missing", "frozen image file is missing")
        media_resp = transport(
            "POST",
            urljoin(config.base_url + "/", "wp-json/wp/v2/media"),
            files={"file": (path.name, path.read_bytes())},
            auth=(config.username, config.app_password),
        )
        if not media_resp.get("ok"):
            raise WordPressPublishError(
                "media_failed",
                sanitize_wp_error(str(media_resp.get("error") or "media upload failed"), secrets),
            )
        payload = media_resp.get("payload") or {}
        media_id = payload.get("id")
    body = {
        "title": article.get("headline") or article.get("seo_title") or "",
        "slug": article.get("slug") or "",
        "content": article.get("article_body") or "",
        "excerpt": article.get("dek") or article.get("meta_description") or "",
        "status": status,
        "meta": {
            "description": article.get("meta_description") or "",
        },
    }
    if media_id is not None:
        body["featured_media"] = media_id
    post_resp = transport(
        "POST",
        urljoin(config.base_url + "/", "wp-json/wp/v2/posts"),
        json=body,
        auth=(config.username, config.app_password),
    )
    if not post_resp.get("ok"):
        raise WordPressPublishError(
            "post_failed",
            sanitize_wp_error(str(post_resp.get("error") or "post failed"), secrets),
        )
    payload = post_resp.get("payload") or {}
    post_id = payload.get("id")
    try:
        post_id_int = int(post_id) if post_id is not None else 0
    except (TypeError, ValueError):
        post_id_int = 0
    if post_id_int <= 0:
        raise WordPressPublishError(
            "missing_post_id",
            "WordPress did not return a valid post ID",
        )
    url = payload.get("link") or payload.get("url")
    if not isinstance(url, str) or not url.startswith("http"):
        raise WordPressPublishError("missing_url", "WordPress did not return a public URL")
    return {
        "ok": True,
        "post_id": post_id_int,
        "url": url,
        "media_id": media_id,
    }


def build_live_wordpress_transport(*, timeout_seconds: int = 60) -> Transport:
    """Live WordPress REST transport. Never logs credentials.

    Uses one persistent requests.Session so cookies survive across WP calls.
    Does not solve or bypass SiteGround captcha — only detects it and fails closed.
    """
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = NEWSAGENT_WP_USER_AGENT

    def transport(
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = session.request(
                method=method,
                url=url,
                json=json,
                files=files,
                auth=auth,
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            return {"ok": False, "error": str(exc), "payload": None, "error_code": "transport_error"}

        body_text = response.text or ""
        if is_siteground_captcha_response(
            status_code=response.status_code,
            headers=response.headers,
            body_text=body_text,
        ):
            return {
                "ok": False,
                "error": "SiteGround sg-captcha challenge (not a WordPress REST response)",
                "payload": None,
                "status_code": response.status_code,
                "error_code": "sg_captcha",
            }

        # HTTP 202 is not a successful WordPress create/update for this client.
        if response.status_code == 202:
            return {
                "ok": False,
                "error": "HTTP 202: not a WordPress JSON success response",
                "payload": {"raw": body_text[:500]},
                "status_code": 202,
                "error_code": "non_json_or_accepted",
            }

        try:
            payload = response.json()
        except ValueError:
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: non-JSON WordPress response",
                "payload": {"raw": body_text[:500]},
                "status_code": response.status_code,
                "error_code": "non_json",
            }

        if response.status_code >= 400:
            detail = payload if isinstance(payload, dict) else {"raw": str(payload)[:300]}
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: {message}",
                "payload": payload,
                "status_code": response.status_code,
                "error_code": "http_error",
            }

        if not isinstance(payload, (dict, list)):
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: unexpected JSON payload type",
                "payload": payload,
                "status_code": response.status_code,
                "error_code": "unexpected_payload",
            }

        return {"ok": True, "payload": payload, "status_code": response.status_code}

    return transport
