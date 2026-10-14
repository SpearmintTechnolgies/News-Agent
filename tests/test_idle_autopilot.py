"""Idle autopilot settings and activity clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsagent_v2.control.idle_autopilot import (
    bootstrap_activity_if_empty,
    is_idle,
    last_activity_at,
    load_settings,
    maybe_tick,
    record_publish,
    record_run,
)


def test_settings_are_editable_from_env(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "NEWSAGENT_V6_IDLE_AUTO_ENABLED": "true",
            "NEWSAGENT_V6_IDLE_AUTO_HOURS": "12",
            "NEWSAGENT_V6_IDLE_AUTO_COUNT": "6",
            "NEWSAGENT_V6_IDLE_AUTO_CHECK_SECONDS": "60",
        },
        settings_path=tmp_path / "missing.json",
    )
    assert settings.enabled is True
    assert settings.idle_hours == 12
    assert settings.article_count == 6
    assert settings.check_every_seconds == 60


def test_disabled_when_count_zero(tmp_path: Path) -> None:
    settings = load_settings(
        {"NEWSAGENT_V6_IDLE_AUTO_COUNT": "0"},
        settings_path=tmp_path / "missing.json",
    )
    assert settings.enabled is False


def test_telegram_overrides_persist(tmp_path: Path) -> None:
    from newsagent_v2.control.idle_autopilot import (
        set_article_count,
        set_enabled,
        set_idle_hours,
        settings_keyboard,
        settings_menu_text,
    )

    path = tmp_path / "idle_settings.json"
    set_idle_hours(48, path=path)
    set_article_count(6, path=path)
    settings = load_settings({"NEWSAGENT_V6_IDLE_AUTO_HOURS": "24"}, settings_path=path)
    assert settings.idle_hours == 48
    assert settings.article_count == 6
    set_enabled(False, path=path)
    settings = load_settings(settings_path=path)
    assert settings.enabled is False
    text = settings_menu_text(settings)
    assert "OFF" in text and "48" in text and "6" in text
    keyboard = settings_keyboard(settings)
    rows = keyboard["inline_keyboard"]
    assert any(btn.get("callback_data") == "idle:hours:48" for btn in rows[1])
    assert any(btn.get("callback_data") == "idle:count:6" for btn in rows[2])


def test_bootstrap_then_not_idle(tmp_path: Path) -> None:
    path = tmp_path / "idle.json"
    settings = load_settings(
        {"NEWSAGENT_V6_IDLE_AUTO_HOURS": "24", "NEWSAGENT_V6_IDLE_AUTO_COUNT": "4"},
        settings_path=tmp_path / "settings.json",
    )
    assert bootstrap_activity_if_empty(path=path) is True
    assert is_idle(settings, path=path) is False
    assert bootstrap_activity_if_empty(path=path) is False


def test_idle_after_hours(tmp_path: Path) -> None:
    path = tmp_path / "idle.json"
    record_run("site-a", path=path)
    data_time = last_activity_at(path)
    assert data_time is not None
    settings = load_settings(
        {"NEWSAGENT_V6_IDLE_AUTO_HOURS": "24", "NEWSAGENT_V6_IDLE_AUTO_COUNT": "4"},
        settings_path=tmp_path / "settings.json",
    )
    now = data_time + timedelta(hours=25)
    assert is_idle(settings, path=path, now=now) is True
    assert is_idle(settings, path=path, now=data_time + timedelta(hours=1)) is False


def test_publish_resets_idle(tmp_path: Path) -> None:
    path = tmp_path / "idle.json"
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    path.write_text(
        '{"last_run_at": "%s", "last_publish_at": "%s"}' % (old.isoformat(), old.isoformat()),
        encoding="utf-8",
    )
    settings = load_settings(
        {"NEWSAGENT_V6_IDLE_AUTO_HOURS": "24", "NEWSAGENT_V6_IDLE_AUTO_COUNT": "4"},
        settings_path=tmp_path / "settings.json",
    )
    assert is_idle(settings, path=path) is True
    record_publish("site-a", path=path)
    assert is_idle(settings, path=path) is False


def test_maybe_tick_starts_once(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "idle.json"
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    path.write_text(
        '{"last_run_at": "%s", "last_publish_at": "%s"}' % (old.isoformat(), old.isoformat()),
        encoding="utf-8",
    )
    gate = __import__("threading").Event()
    started = []

    def run_fn(settings):
        started.append(settings.article_count)
        gate.set()

    env = {
        "NEWSAGENT_V6_IDLE_AUTO_ENABLED": "true",
        "NEWSAGENT_V6_IDLE_AUTO_HOURS": "24",
        "NEWSAGENT_V6_IDLE_AUTO_COUNT": "4",
        "NEWSAGENT_V6_IDLE_AUTO_CHECK_SECONDS": "1",
    }
    import newsagent_v2.control.idle_autopilot as mod

    monkeypatch.setattr(mod, "_last_check_monotonic", 0.0)
    monkeypatch.setattr(mod, "_running", False)
    result = maybe_tick(environ=env, run_fn=run_fn, path=path, force=True)
    assert result.get("started") is True
    assert gate.wait(timeout=2.0)
    assert started == [4]
    # Second tick while still "busy" should refuse (force running lock)
    monkeypatch.setattr(mod, "_running", True)
    again = maybe_tick(environ=env, run_fn=run_fn, path=path, force=True)
    assert again.get("reason") == "already_running"
    monkeypatch.setattr(mod, "_running", False)
