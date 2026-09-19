"""Pre-send validation for V2 Telegram TEST delivery. Critical failure → no send."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from newsagent_v2.telegram.config import TelegramConfig, mask_token
from newsagent_v2.telegram.contract import ALLOWED_URL_SCHEMES, TelegramOutbound
from newsagent_v2.telegram.formatter import WINDOWS_PATH_RE, format_outbound_text

REPO_ROOT = Path(__file__).resolve().parents[3]

SECRET_NAME_RE = re.compile(
    r"\b(?:NEWSAGENT_V2_TELEGRAM_BOT_TOKEN|GROQ_API_KEY|OPENAI_API_KEY|"
    r"AUTHORIZATION|API_KEY)\b",
    re.IGNORECASE,
)
BOT_TOKEN_SHAPE_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
LOCALHOST_RE = re.compile(r"\b(?:localhost|127\.0\.0\.1)\b", re.IGNORECASE)
PROMPT_LEAK_RE = re.compile(
    r"(you are the editorial decision engine|system prompt|ignore previous instructions)",
    re.IGNORECASE,
)
STACK_RE = re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE)


class TelegramValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_repo_path(path: Path, *, repo_root: Path = REPO_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = repo_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TelegramValidationError(
            "image_outside_repo",
            "image_path must be inside the NewsAgent-V2 repository",
        ) from exc
    return resolved


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise TelegramValidationError("malformed_url", "article_url scheme is not allowed")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1"} or host.endswith(".localhost"):
        raise TelegramValidationError("localhost_url", "article_url must not be localhost")


def _scan_outgoing_text(text: str, *, token: str) -> None:
    if WINDOWS_PATH_RE.search(text):
        raise TelegramValidationError(
            "windows_path_leak",
            "outgoing Telegram text contains an internal Windows path",
        )
    if SECRET_NAME_RE.search(text) or BOT_TOKEN_SHAPE_RE.search(text):
        raise TelegramValidationError("secret_leak", "outgoing Telegram text contains a secret")
    if token and token in text:
        raise TelegramValidationError("secret_leak", "outgoing Telegram text contains the bot token")
    if LOCALHOST_RE.search(text):
        raise TelegramValidationError("localhost_leak", "outgoing Telegram text contains localhost")
    if PROMPT_LEAK_RE.search(text) or STACK_RE.search(text):
        raise TelegramValidationError(
            "prompt_or_stack_leak",
            "outgoing Telegram text looks like prompt or stack leakage",
        )


def validate_outbound(
    payload: TelegramOutbound,
    config: TelegramConfig,
    *,
    repo_root: Path = REPO_ROOT,
) -> dict[str, object]:
    if not config.test_mode:
        raise TelegramValidationError("test_mode_required", "test_mode must be True")
    if payload.mode != "test":
        raise TelegramValidationError("test_mode_required", "payload.mode must be 'test'")

    destination = (payload.chat_id or config.test_chat_id).strip()
    if destination != config.test_chat_id:
        raise TelegramValidationError(
            "wrong_chat_id",
            "destination chat_id does not match NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID",
        )

    headline = (payload.headline or "").strip()
    if not headline:
        raise TelegramValidationError("empty_headline", "headline is empty")

    if payload.article_url:
        _check_url(payload.article_url.strip())

    image_path: Path | None = None
    send_photo = False
    raw_image = (payload.image_path or "").strip()
    if raw_image:
        candidate = Path(raw_image)
        image_path = resolve_repo_path(candidate, repo_root=repo_root)
        if not image_path.is_file():
            raise TelegramValidationError(
                "image_missing",
                "image_path is inside the repo but the file does not exist",
            )
        send_photo = True

    formatted = format_outbound_text(payload, caption=send_photo)
    text = str(formatted["text"])
    _scan_outgoing_text(text, token=config.bot_token)
    if image_path is not None and str(image_path) in text:
        raise TelegramValidationError(
            "image_path_in_caption",
            "image filesystem path must not appear in Telegram caption",
        )
    _scan_outgoing_text(headline, token=config.bot_token)
    if payload.dek:
        _scan_outgoing_text(payload.dek, token=config.bot_token)

    return {
        "ok": True,
        "chat_id": destination,
        "send_photo": send_photo,
        "image_path": str(image_path) if image_path else None,
        "formatted": formatted,
        "token_fingerprint": mask_token(config.bot_token),
    }
