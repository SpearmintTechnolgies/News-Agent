"""Boolean-only credential inspection. Never returns secret values."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from newsagent_v2.image.providers.gemini import KEY_ENV as GEMINI_KEY_ENV

GROQ_KEY_ENV = "GROQ_API_KEY"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
LEGACY_GEMINI_KEY_ENV = "GEMINI_API_KEY"
QWEN_KEY_ENV = "NEWSAGENT_V2_QWEN_API_KEY"
QWEN_BASE_ENV = "NEWSAGENT_V2_QWEN_BASE_URL"
BEDROCK_MANTLE_KEY_ENV = "NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY"


def _present(environ: dict[str, str], name: str) -> bool:
    return bool(str(environ.get(name) or "").strip())


def merge_windows_user_env(environ: dict[str, str], names: tuple[str, ...]) -> dict[str, str]:
    if os.name != "nt":
        return environ
    try:
        import winreg
    except ImportError:
        return environ
    merged = dict(environ)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            for name in names:
                if str(merged.get(name, "")).strip():
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if value:
                    merged[name] = str(value)
    except OSError:
        return merged
    return merged


def load_environ(repo_root: Path | None = None) -> dict[str, str]:
    environ = {key: str(value) for key, value in os.environ.items()}
    if repo_root is not None:
        from dotenv import load_dotenv

        load_dotenv(repo_root / ".env")
        environ = {key: str(value) for key, value in os.environ.items()}
    environ = merge_windows_user_env(
        environ,
        (GROQ_KEY_ENV, GEMINI_KEY_ENV, QWEN_KEY_ENV, QWEN_BASE_ENV, BEDROCK_MANTLE_KEY_ENV),
    )
    return environ


def credential_flags(environ: dict[str, str] | None = None) -> dict[str, Any]:
    source = environ if environ is not None else {key: str(value) for key, value in os.environ.items()}
    return {
        GROQ_KEY_ENV: _present(source, GROQ_KEY_ENV),
        GEMINI_KEY_ENV: _present(source, GEMINI_KEY_ENV),
        LEGACY_GEMINI_KEY_ENV: _present(source, LEGACY_GEMINI_KEY_ENV),
        OPENROUTER_KEY_ENV: _present(source, OPENROUTER_KEY_ENV),
        QWEN_KEY_ENV: _present(source, QWEN_KEY_ENV),
        QWEN_BASE_ENV: _present(source, QWEN_BASE_ENV),
        BEDROCK_MANTLE_KEY_ENV: _present(source, BEDROCK_MANTLE_KEY_ENV),
        "openrouter_used_by_v2_article_writer": False,
        "gemini_image_adapter_exists": True,
        "gemini_text_article_adapter_in_live_make": False,
    }
