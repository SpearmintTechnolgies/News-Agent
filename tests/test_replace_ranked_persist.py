"""Offered HIGH TREND / fill cards must be on disk before RUN STORY."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.research.fetch import BrowserFetcher
from newsagent_v2.telegram.v5_callbacks import V5TelegramStore


def test_replace_ranked_persists_events_for_run_story(tmp_path: Path) -> None:
    store = EventStore(root=tmp_path / "events")
    pipeline = V5DiscoveryPipeline.__new__(V5DiscoveryPipeline)
    pipeline.event_store = store
    pipeline.telegram_store = V5TelegramStore()
    pipeline._ranked_events = []

    event = NewsEvent(
        event_id="evt-high-trend-late",
        canonical_title="Late search card",
    )
    pipeline.replace_ranked([event])

    assert store.get("evt-high-trend-late") is not None
    assert pipeline.get_ranked_events()[0].event_id == "evt-high-trend-late"


def test_browser_failure_is_remembered_process_wide(monkeypatch) -> None:
    BrowserFetcher._process_disabled_reason = ""

    class FakePw:
        def start(self):
            raise RuntimeError("TargetClosedError: BrowserType.launch closed")

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePw()
    playwright = types.ModuleType("playwright")
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    first = BrowserFetcher()
    assert first._ensure() is False
    assert "TargetClosedError" in BrowserFetcher._process_disabled_reason

    second = BrowserFetcher()
    assert second.disabled_reason == BrowserFetcher._process_disabled_reason
    assert second._ensure() is False

    BrowserFetcher._process_disabled_reason = ""
