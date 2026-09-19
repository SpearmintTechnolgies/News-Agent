"""Local CEO Vertex env overlay for the V2 listener.

Fills missing Vertex configuration from known local paths only.
Never embeds service-account JSON contents. Never copies credentials into the repo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# Non-secret local CEO defaults used when the live listener process has no Vertex env.
LOCAL_CEO_VERTEX_PROJECT = "prefab-segment-500506-j4"
LOCAL_CEO_VERTEX_LOCATION = "global"
LOCAL_CEO_VERTEX_MODEL = "gemini-3.1-flash-image"

# Prefer the Telegram Desktop path confirmed for the live CEO machine; fall back to Downloads.
_LOCAL_CEO_CREDENTIAL_CANDIDATES: tuple[Path, ...] = (
    Path(r"C:\Users\Global\Downloads\Telegram Desktop\prefab-segment-500506-j4-8738a236f43e.json"),
    Path(r"C:\Users\Global\Downloads\prefab-segment-500506-j4-8738a236f43e.json"),
)

VERTEX_ENV_KEYS: tuple[str, ...] = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "NEWSAGENT_V2_VERTEX_PROJECT",
    "NEWSAGENT_V2_VERTEX_LOCATION",
    "NEWSAGENT_V2_VERTEX_MODEL",
    "NEWSAGENT_V2_VERTEX_ENABLED",
)


def resolve_local_ceo_credential_path() -> Path | None:
    """Return the first existing local credential file path, or None."""
    for path in _LOCAL_CEO_CREDENTIAL_CANDIDATES:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _nonempty(environ: Mapping[str, str], key: str) -> str:
    return str(environ.get(key, "") or "").strip()


def local_ceo_vertex_defaults() -> dict[str, str] | None:
    """Build non-secret defaults when the local credential file exists."""
    cred = resolve_local_ceo_credential_path()
    if cred is None:
        return None
    return {
        "GOOGLE_APPLICATION_CREDENTIALS": str(cred),
        "NEWSAGENT_V2_VERTEX_PROJECT": LOCAL_CEO_VERTEX_PROJECT,
        "NEWSAGENT_V2_VERTEX_LOCATION": LOCAL_CEO_VERTEX_LOCATION,
        "NEWSAGENT_V2_VERTEX_MODEL": LOCAL_CEO_VERTEX_MODEL,
        "NEWSAGENT_V2_VERTEX_ENABLED": "true",
    }


def _persist_user_env_if_missing(defaults: Mapping[str, str]) -> None:
    """Persist missing Vertex keys to HKCU User env so future shells inherit them."""
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            for name, value in defaults.items():
                try:
                    existing, _ = winreg.QueryValueEx(key, name)
                    if str(existing or "").strip():
                        continue
                except FileNotFoundError:
                    pass
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
                # Also expose to this process so subsequent reads see it immediately.
                os.environ.setdefault(name, str(value))
    except OSError:
        return


def sync_vertex_process_environ(environ: Mapping[str, str]) -> dict[str, bool]:
    """
    Mirror Vertex keys from an application environ dict into process os.environ.

    google.auth.default() reads process env, not an application dict.
    Returns a map of key -> present_in_process_after_sync.
    """
    present: dict[str, bool] = {}
    for key in VERTEX_ENV_KEYS:
        val = _nonempty(environ, key)
        if val:
            os.environ[key] = val
        present[key] = bool(str(os.environ.get(key, "") or "").strip())
    return present


def apply_local_vertex_environ(
    environ: Mapping[str, str],
    *,
    persist_user_env: bool = False,
) -> dict[str, str]:
    """
    Return a copy of environ with missing Vertex keys filled from local CEO defaults.

    Only applies when the local credential file exists. Does not overwrite set values.
    Always mirrors Vertex keys into os.environ so google.auth.default() can resolve
    GOOGLE_APPLICATION_CREDENTIALS (ADC reads process env, not the dict alone).
    """
    out = {str(k): str(v) for k, v in environ.items()}
    defaults = local_ceo_vertex_defaults()
    if defaults is not None:
        for key, value in defaults.items():
            if not _nonempty(out, key):
                out[key] = value
        if persist_user_env:
            _persist_user_env_if_missing(defaults)
    sync_vertex_process_environ(out)
    return out
