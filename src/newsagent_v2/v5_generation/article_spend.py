"""Every Kimi attempt for one story, including discarded rewrites and later revises."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STORIES_ROOT = Path("output/v5_stories")


def _path(event_id: str, root: Path | None = None) -> Path:
    return Path(root or STORIES_ROOT) / f"STORY-{event_id}" / "text_spend.json"


def _int(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def spend_kind(stage: str) -> str:
    """The draft is content. Every later rewrite is orchestration."""
    name = str(stage or "").lower()
    if name.startswith("revise") or "orchestr" in name:
        return "orchestration"
    return "content"


def record_text_spend(event_id: str, usage: dict[str, Any], root: Path | None = None) -> None:
    """Append one write attempt. A failed attempt is kept so the card can price it.

    When the attempt carries a per-call log, each call is stored on its own
    so the review card can price content and orchestration separately.
    """
    log = usage.get("log") if isinstance(usage.get("log"), list) else []
    entries = [row for row in log if isinstance(row, dict)] or [usage]
    path = _path(event_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[Any] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                rows = loaded
        except (OSError, json.JSONDecodeError):
            rows = []
    wrote = False
    for entry in entries:
        calls = _int(entry, "calls", "requests", "request_count") or (1 if entry is not usage else 0)
        if entry is usage:
            calls = _int(usage, "calls", "requests", "request_count")
        prompt = _int(entry, "prompt_tokens", "input_tokens")
        completion = _int(entry, "completion_tokens", "output_tokens")
        if calls <= 0 and prompt <= 0 and completion <= 0:
            continue
        stage = str(entry.get("stage") or ("draft" if entry is not usage else ""))
        rows.append({
            "kind": spend_kind(stage),
            "stage": stage,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "calls": calls or 1,
            "provider": str(usage.get("provider") or entry.get("provider") or ""),
            "model": str(usage.get("model") or entry.get("model") or ""),
        })
        wrote = True
    if wrote:
        path.write_text(json.dumps(rows), encoding="utf-8")


def total_text_spend(event_id: str, root: Path | None = None) -> dict[str, Any]:
    """Sum of every recorded attempt for this story."""
    path = _path(event_id, root)
    if not path.is_file():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(rows, list):
        return {}
    prompt = completion = calls = 0
    provider = model = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        prompt += _int(row, "prompt_tokens", "input_tokens")
        completion += _int(row, "completion_tokens", "output_tokens")
        calls += _int(row, "calls", "requests", "request_count")
        provider = str(row.get("provider") or provider)
        model = str(row.get("model") or model)
    if calls <= 0 and prompt <= 0 and completion <= 0:
        return {}
    attempts = [row for row in rows if isinstance(row, dict)]
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "calls": calls,
        "requests": calls,
        "request_count": calls,
        "provider": provider,
        "model": model,
        "attempts": attempts,
    }
