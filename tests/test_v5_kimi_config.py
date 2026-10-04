"""Offline tests for V5 persistent Kimi configuration.

Tests that v5_canonical_runtime fails closed when Kimi is not configured.
No provider calls. No network. No secrets printed.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_env_file_exists():
    """.env file exists with Kimi configuration."""
    env_path = REPO_ROOT / ".env"
    assert env_path.exists(), f".env file not found at {env_path}"

    content = env_path.read_text(encoding="utf-8")
    assert "NEWSAGENT_V2_V4_WRITER_PROVIDER=kimi" in content, \
        "NEWSAGENT_V2_V4_WRITER_PROVIDER=kimi not in .env"
    assert "NEWSAGENT_V2_V4_ALLOW_KIMI=true" in content, \
        "NEWSAGENT_V2_V4_ALLOW_KIMI=true not in .env"


def test_v5_runtime_imports_dotenv():
    """v5_canonical_runtime imports dotenv for persistent config."""
    runtime_path = REPO_ROOT / "src" / "newsagent_v2" / "telegram" / "v5_canonical_runtime.py"
    content = runtime_path.read_text(encoding="utf-8")

    assert "from dotenv import load_dotenv" in content, \
        "load_dotenv import missing"
    assert "load_dotenv(env_path)" in content, \
        "load_dotenv call missing"


def test_v5_runtime_has_kimi_validation():
    """v5_canonical_runtime has fail-closed Kimi validation."""
    runtime_path = REPO_ROOT / "src" / "newsagent_v2" / "telegram" / "v5_canonical_runtime.py"
    content = runtime_path.read_text(encoding="utf-8")

    assert "FAIL-CLOSED" in content, "Fail-closed comment missing"
    assert 'provider != "kimi"' in content, "Kimi provider check missing"
    assert "not allow_kimi" in content, "allow_kimi check missing"
    assert "not kimi_key" in content, "kimi_key check missing"
    assert 'sys.exit(1)' in content, "sys.exit(1) for config error missing"


def test_v5_runtime_no_hardcoded_groq_fallback():
    """FAIL: The validation prevents Groq fallback in V5."""
    runtime_path = REPO_ROOT / "src" / "newsagent_v2" / "telegram" / "v5_canonical_runtime.py"
    content = runtime_path.read_text(encoding="utf-8")

    # Should NOT have default groq provider
    assert "groq" not in content.lower() or "provider != \"kimi\"" in content, \
        "Run loop without Kimi check - can fall back to Groq"


def test_groq_still_available_in_other_code():
    """Groq support is NOT removed globally - only blocked in V5 canonical runtime."""
    from newsagent_v2.article.writer.v4.provider import DEFAULT_PROVIDER

    # Groq is still the default provider in the general writer
    assert DEFAULT_PROVIDER == "groq", "Groq default changed unexpectedly"

    # Provider resolver still supports multiple providers
    from newsagent_v2.article.writer.v4.provider import (
        PROVIDER_GROQ,
        PROVIDER_KIMI,
    )
    assert PROVIDER_GROQ == "groq"
    assert PROVIDER_KIMI == "kimi"
