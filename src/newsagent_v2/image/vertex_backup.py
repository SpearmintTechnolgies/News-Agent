"""Backup Vertex credential for the image path.

A second GCP service account (its own project, its own daily cap) that is tried
when the primary Vertex is over its daily cap or is rejected for rate limit /
quota (HTTP 429, RESOURCE_EXHAUSTED). If the backup also fails, the Nova Canvas
fallback runs as before.

Env:
  NEWSAGENT_V2_VERTEX_BACKUP_ENABLED          true/false (default false)
  NEWSAGENT_V2_VERTEX_BACKUP_CREDENTIALS      path to the backup service-account JSON
  NEWSAGENT_V2_VERTEX_BACKUP_PROJECT          GCP project id of the backup account
  NEWSAGENT_V2_VERTEX_BACKUP_LOCATION         Vertex location (default "global")
  NEWSAGENT_V2_VERTEX_BACKUP_MODEL            optional; defaults to NEWSAGENT_V2_VERTEX_MODEL
  NEWSAGENT_V2_MAX_VERTEX_BACKUP_CALLS_PER_DAY  backup daily cap (default 25)

The backup never touches the process GOOGLE_APPLICATION_CREDENTIALS (that stays the
primary's), never counts against the primary Vertex ledger, and never logs or
persists credential contents or tokens.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.image.providers.vertex_nano_banana import (
    MODEL_ALIAS_ENV,
    MODEL_ENV,
    VertexConfigError,
    VertexNanoBananaConfig,
)
from newsagent_v2.image.vertex_budget import (
    DEFAULT_BUDGET_ROOT,
    _FILE_LOCK,
    _read_json,
    _utc_day,
    _write_json_atomic,
)

ENV_BACKUP_ENABLED = "NEWSAGENT_V2_VERTEX_BACKUP_ENABLED"
ENV_BACKUP_CREDENTIALS = "NEWSAGENT_V2_VERTEX_BACKUP_CREDENTIALS"
ENV_BACKUP_PROJECT = "NEWSAGENT_V2_VERTEX_BACKUP_PROJECT"
ENV_BACKUP_LOCATION = "NEWSAGENT_V2_VERTEX_BACKUP_LOCATION"
ENV_BACKUP_MODEL = "NEWSAGENT_V2_VERTEX_BACKUP_MODEL"
ENV_BACKUP_MAX_DAY = "NEWSAGENT_V2_MAX_VERTEX_BACKUP_CALLS_PER_DAY"

BACKUP_MAX_CALLS_PER_DAY_DEFAULT = 25
BACKUP_LABEL = "vertex_backup"

CODE_BACKUP_DISABLED = "vertex_backup_disabled"
CODE_BACKUP_CONFIG = "vertex_backup_config_incomplete"
CODE_BACKUP_DAILY_EXHAUSTED = "VERTEX_BACKUP_DAILY_BUDGET_EXHAUSTED"


class VertexBackupConfigError(VertexConfigError):
    """Missing/invalid backup configuration. Never includes secret values."""


def _nonempty(environ: dict[str, str] | None, key: str) -> str:
    return str((environ or {}).get(key) or "").strip()


def vertex_backup_enabled(environ: dict[str, str] | None) -> bool:
    return _nonempty(environ, ENV_BACKUP_ENABLED).lower() in {"1", "true", "yes", "on"}


def max_backup_daily_calls(environ: dict[str, str] | None) -> int:
    raw = _nonempty(environ, ENV_BACKUP_MAX_DAY)
    if not raw:
        return BACKUP_MAX_CALLS_PER_DAY_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return BACKUP_MAX_CALLS_PER_DAY_DEFAULT


def load_vertex_backup_config(environ: dict[str, str] | None) -> tuple[VertexNanoBananaConfig, str]:
    """Return (config, credentials_path). Raises VertexBackupConfigError (no secrets)."""
    missing: list[str] = []
    cred = _nonempty(environ, ENV_BACKUP_CREDENTIALS)
    project = _nonempty(environ, ENV_BACKUP_PROJECT)
    location = _nonempty(environ, ENV_BACKUP_LOCATION) or "global"
    model = (
        _nonempty(environ, ENV_BACKUP_MODEL)
        or _nonempty(environ, MODEL_ENV)
        or _nonempty(environ, MODEL_ALIAS_ENV)
    )
    if not cred:
        missing.append(ENV_BACKUP_CREDENTIALS)
    elif not Path(cred).is_file():
        missing.append(f"{ENV_BACKUP_CREDENTIALS} (file not found)")
    if not project:
        missing.append(ENV_BACKUP_PROJECT)
    if not model:
        missing.append(ENV_BACKUP_MODEL)
    if missing:
        raise VertexBackupConfigError("Vertex backup configuration incomplete: " + ", ".join(missing))
    config = VertexNanoBananaConfig(
        project=project,
        location=location,
        model=model,
        credentials_path_set=True,
        auth_mode="service_account_file",
    )
    return config, cred


_TOKEN_LOCK = threading.Lock()
_CRED_CACHE: dict[str, Any] = {}


def backup_access_token_provider(credentials_path: str) -> Callable[[], str]:
    """Token provider bound to the backup SA file (explicit, not ADC)."""

    def _token() -> str:
        try:
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2 import service_account
        except Exception as exc:  # noqa: BLE001
            raise VertexBackupConfigError("python_package:google-auth is required for Vertex backup") from exc
        with _TOKEN_LOCK:
            creds = _CRED_CACHE.get(credentials_path)
            if creds is None:
                try:
                    creds = service_account.Credentials.from_service_account_file(
                        credentials_path,
                        scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    )
                except Exception as exc:  # noqa: BLE001
                    raise VertexBackupConfigError(
                        f"backup credential file unreadable ({exc.__class__.__name__})"
                    ) from exc
                _CRED_CACHE[credentials_path] = creds
            if not getattr(creds, "valid", False):
                creds.refresh(GoogleAuthRequest())
            token = getattr(creds, "token", None)
        if not isinstance(token, str) or not token.strip():
            raise VertexBackupConfigError("failed to obtain Vertex backup access token")
        return token

    return _token


class VertexBackupDailyBudget:
    """Durable per-UTC-day call counter for the backup account (separate from the primary ledger)."""

    def __init__(self, *, root: Path | None = None, environ: dict[str, str] | None = None) -> None:
        self.root = (root or DEFAULT_BUDGET_ROOT) / "backup_daily"
        self.environ = dict(environ or {})

    def _file(self) -> Path:
        return self.root / f"{_utc_day()}.json"

    def _load(self) -> dict[str, Any]:
        data = _read_json(self._file())
        day = _utc_day()
        if not data or data.get("day") != day:
            return {"day": day, "vertex_backup_calls": 0, "reservations": []}
        return data

    def calls_used(self) -> int:
        with _FILE_LOCK:
            return int(self._load().get("vertex_backup_calls") or 0)

    def limit(self) -> int:
        return max_backup_daily_calls(self.environ)

    def try_reserve(self, *, event_id: str, article_hash: str, reason: str) -> tuple[bool, int]:
        """Persist one reservation BEFORE the backup call. Returns (allowed, used_after)."""
        with _FILE_LOCK:
            data = self._load()
            used = int(data.get("vertex_backup_calls") or 0)
            if used >= self.limit():
                return False, used
            data["vertex_backup_calls"] = used + 1
            data.setdefault("reservations", []).append(
                {
                    "event_id": event_id,
                    "article_hash": article_hash,
                    "reason": reason,
                    "reserved_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _write_json_atomic(self._file(), data)
            return True, used + 1
