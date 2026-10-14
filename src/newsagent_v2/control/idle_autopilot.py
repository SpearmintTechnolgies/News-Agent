"""Idle autopilot: if nobody ran or published for N hours, write and publish trend stories.

Defaults from env; Telegram can override via data/v5_state/idle_settings.json:
  NEWSAGENT_V6_IDLE_AUTO_ENABLED=true|false   (default true)
  NEWSAGENT_V6_IDLE_AUTO_HOURS=24             (hours of no RUN and no publish)
  NEWSAGENT_V6_IDLE_AUTO_COUNT=4              (articles per website)
  NEWSAGENT_V6_IDLE_AUTO_CHECK_SECONDS=300    (how often the poll loop may tick)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

ENABLED_ENV = "NEWSAGENT_V6_IDLE_AUTO_ENABLED"
HOURS_ENV = "NEWSAGENT_V6_IDLE_AUTO_HOURS"
COUNT_ENV = "NEWSAGENT_V6_IDLE_AUTO_COUNT"
CHECK_ENV = "NEWSAGENT_V6_IDLE_AUTO_CHECK_SECONDS"
ACTIVITY_PATH = Path("data/v5_state/idle_activity.json")
SETTINGS_PATH = Path("data/v5_state/idle_settings.json")

# Telegram callback prefixes
IDLE_MENU_CALLBACK = "idle:menu"
IDLE_BACK_CALLBACK = "idle:back"
HOUR_CHOICES = (12, 24, 48, 72)
COUNT_CHOICES = (2, 4, 6, 8)

_lock = threading.RLock()
_running = False
_last_check_monotonic = 0.0


@dataclass(frozen=True)
class IdleAutopilotSettings:
    enabled: bool
    idle_hours: float
    article_count: int
    check_every_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "idle_hours": self.idle_hours,
            "article_count": self.article_count,
            "check_every_seconds": self.check_every_seconds,
        }


def _read_overrides(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_overrides(updates: dict[str, Any], *, path: Path = SETTINGS_PATH) -> dict[str, Any]:
    """Merge Telegram edits into the on-disk override file."""
    with _lock:
        data = _read_overrides(path)
        for key, value in updates.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return dict(data)


def load_settings(
    environ: dict[str, str] | None = None,
    *,
    settings_path: Path = SETTINGS_PATH,
) -> IdleAutopilotSettings:
    env = environ if environ is not None else {}
    enabled_raw = str(env.get(ENABLED_ENV, "true") or "true").strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"}
    try:
        hours = float(env.get(HOURS_ENV, "24") or 24)
    except ValueError:
        hours = 24.0
    try:
        count = int(env.get(COUNT_ENV, "4") or 4)
    except ValueError:
        count = 4
    try:
        check_every = float(env.get(CHECK_ENV, "300") or 300)
    except ValueError:
        check_every = 300.0

    overrides = _read_overrides(settings_path)
    if "enabled" in overrides:
        enabled = bool(overrides["enabled"])
    if "idle_hours" in overrides:
        try:
            hours = float(overrides["idle_hours"])
        except (TypeError, ValueError):
            pass
    if "article_count" in overrides:
        try:
            count = int(overrides["article_count"])
        except (TypeError, ValueError):
            pass
    if "check_every_seconds" in overrides:
        try:
            check_every = float(overrides["check_every_seconds"])
        except (TypeError, ValueError):
            pass

    # Explicit off stays off even if hours/count are positive.
    if overrides.get("enabled") is False:
        enabled_final = False
    else:
        enabled_final = bool(enabled) and hours > 0 and count > 0

    return IdleAutopilotSettings(
        enabled=enabled_final,
        idle_hours=max(0.0, hours),
        article_count=max(0, count),
        check_every_seconds=max(30.0, check_every),
    )


def set_enabled(enabled: bool, *, path: Path = SETTINGS_PATH) -> IdleAutopilotSettings:
    save_overrides({"enabled": bool(enabled)}, path=path)
    return load_settings(settings_path=path)


def set_idle_hours(hours: float, *, path: Path = SETTINGS_PATH) -> IdleAutopilotSettings:
    save_overrides({"idle_hours": max(1.0, float(hours))}, path=path)
    return load_settings(settings_path=path)


def set_article_count(count: int, *, path: Path = SETTINGS_PATH) -> IdleAutopilotSettings:
    save_overrides({"article_count": max(1, int(count))}, path=path)
    return load_settings(settings_path=path)


def settings_menu_text(settings: IdleAutopilotSettings) -> str:
    state = "ON" if settings.enabled else "OFF"
    return (
        "🤖 Idle autopilot\n\n"
        f"Status: <b>{state}</b>\n"
        f"Idle after: <b>{settings.idle_hours:g} hours</b> with no RUN STORY and no publish\n"
        f"Articles per website: <b>{settings.article_count}</b>\n\n"
        "Tap a button to change. Changes apply immediately."
    )


def settings_keyboard(settings: IdleAutopilotSettings) -> dict[str, Any]:
    hours_row = [
        {
            "text": f"{'✓ ' if abs(settings.idle_hours - h) < 0.01 else ''}{h}h",
            "callback_data": f"idle:hours:{h}",
        }
        for h in HOUR_CHOICES
    ]
    count_row = [
        {
            "text": f"{'✓ ' if settings.article_count == n else ''}{n}",
            "callback_data": f"idle:count:{n}",
        }
        for n in COUNT_CHOICES
    ]
    on_mark = "✓ " if settings.enabled else ""
    off_mark = "✓ " if not settings.enabled else ""
    return {
        "inline_keyboard": [
            [
                {"text": f"{on_mark}ON", "callback_data": "idle:on"},
                {"text": f"{off_mark}OFF", "callback_data": "idle:off"},
            ],
            hours_row,
            count_row,
            [{"text": "Back to websites", "callback_data": IDLE_BACK_CALLBACK}],
        ]
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_activity(path: Path = ACTIVITY_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_activity(payload: dict[str, Any], path: Path = ACTIVITY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def record_run(site_id: str = "", *, path: Path = ACTIVITY_PATH) -> None:
    with _lock:
        data = _read_activity(path)
        data["last_run_at"] = _now_iso()
        if site_id:
            sites = data.setdefault("sites", {})
            if not isinstance(sites, dict):
                sites = {}
                data["sites"] = sites
            row = sites.setdefault(site_id, {})
            if not isinstance(row, dict):
                row = {}
                sites[site_id] = row
            row["last_run_at"] = data["last_run_at"]
        _write_activity(data, path)


def record_publish(site_id: str = "", *, path: Path = ACTIVITY_PATH) -> None:
    with _lock:
        data = _read_activity(path)
        data["last_publish_at"] = _now_iso()
        if site_id:
            sites = data.setdefault("sites", {})
            if not isinstance(sites, dict):
                sites = {}
                data["sites"] = sites
            row = sites.setdefault(site_id, {})
            if not isinstance(row, dict):
                row = {}
                sites[site_id] = row
            row["last_publish_at"] = data["last_publish_at"]
        _write_activity(data, path)


def record_autopilot(*, path: Path = ACTIVITY_PATH) -> None:
    with _lock:
        data = _read_activity(path)
        data["last_autopilot_at"] = _now_iso()
        _write_activity(data, path)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def last_activity_at(path: Path = ACTIVITY_PATH) -> datetime | None:
    """Latest of last RUN and last publish (global)."""
    data = _read_activity(path)
    stamps = [_parse_iso(data.get("last_run_at")), _parse_iso(data.get("last_publish_at"))]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return None
    return max(stamps)


def bootstrap_activity_if_empty(*, path: Path = ACTIVITY_PATH) -> bool:
    """First boot: start the idle clock now so autopilot does not fire immediately."""
    with _lock:
        data = _read_activity(path)
        if data.get("last_run_at") or data.get("last_publish_at") or data.get("bootstrapped_at"):
            return False
        now = _now_iso()
        data["bootstrapped_at"] = now
        data["last_run_at"] = now
        data["last_publish_at"] = now
        _write_activity(data, path)
        return True


def is_idle(settings: IdleAutopilotSettings, *, path: Path = ACTIVITY_PATH, now: datetime | None = None) -> bool:
    if not settings.enabled:
        return False
    bootstrap_activity_if_empty(path=path)
    stamp = last_activity_at(path)
    current = now or datetime.now(timezone.utc)
    if stamp is None:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_hours = (current - stamp).total_seconds() / 3600.0
    return age_hours >= settings.idle_hours


def is_busy() -> bool:
    with _lock:
        return _running


def maybe_tick(
    *,
    environ: dict[str, str],
    run_fn: Callable[[IdleAutopilotSettings], None],
    path: Path = ACTIVITY_PATH,
    force: bool = False,
) -> dict[str, Any]:
    """If idle long enough, start one autopilot pass in a background thread."""
    global _last_check_monotonic, _running
    settings = load_settings(environ)
    if not settings.enabled:
        return {"ok": False, "reason": "disabled", "settings": settings.to_dict()}
    now_m = time.monotonic()
    if not is_idle(settings, path=path):
        return {
            "ok": False,
            "reason": "not_idle",
            "last_activity": (last_activity_at(path) or datetime.min.replace(tzinfo=timezone.utc)).isoformat(),
            "settings": settings.to_dict(),
        }
    with _lock:
        if _running:
            return {"ok": False, "reason": "already_running", "settings": settings.to_dict()}
        if not force and (now_m - _last_check_monotonic) < settings.check_every_seconds:
            return {"ok": False, "reason": "check_cooldown", "settings": settings.to_dict()}
        _last_check_monotonic = now_m
        _running = True

    def _wrap() -> None:
        global _running
        try:
            run_fn(settings)
        except Exception:
            logger.exception("[IDLE-AUTOPILOT] failed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=_wrap, daemon=True, name="idle-autopilot").start()
    return {"ok": True, "started": True, "settings": settings.to_dict()}
