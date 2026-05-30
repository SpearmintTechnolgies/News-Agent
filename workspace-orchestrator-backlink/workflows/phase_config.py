"""Load pipeline phase configuration (Phase 1 = approval queue only)."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_PHASE_PATH = Path(__file__).resolve().parent.parent / "config" / "phase.json"


@lru_cache(maxsize=1)
def _phase_from_file() -> int:
    if not _PHASE_PATH.is_file():
        return 1
    try:
        data = json.loads(_PHASE_PATH.read_text(encoding="utf-8"))
        phase = int(data.get("phase", 1))
        return phase if phase in {1, 2} else 1
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 1


def get_phase() -> int:
    env = os.environ.get("BACKLINK_PHASE", "").strip()
    if env:
        try:
            phase = int(env)
            return phase if phase in {1, 2} else 1
        except ValueError:
            pass
    return _phase_from_file()


def reset_phase_cache() -> None:
    _phase_from_file.cache_clear()
