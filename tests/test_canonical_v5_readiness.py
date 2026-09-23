from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from newsagent_v2.discovery.event_clusterer import EventReport, NewsEvent
from newsagent_v2.discovery.niche_filter import RelevanceDecision
from newsagent_v2.telegram.v5_callbacks import V5TelegramStore
from newsagent_v2.telegram.v5_canonical_runtime import CanonicalV5Integration
from newsagent_v2.v5_generation.source_expansion_adapter import ExpansionResult


def _event(event_id: str, ready: bool) -> NewsEvent:
    reports = [
        EventReport(
            report_id=f"{event_id}-1",
            source="Source One",
            source_id="source-one",
            source_authority=0.7,
            headline=f"{event_id} reports a material development in 2026",
            url=f"https://source-one.example/{event_id}",
            published_at="2026-09-22T00:00:00+00:00",
            retrieved_at="2026-09-22T00:00:00+00:00",
            description=(
                "Officials announced a material development in 2026 with measured "
                "results, relevant context, independently reported details, market implications, "
                "timing information, participants, operational background, historical comparison, "
                "implementation details, public response, and additional factual context for readers."
                " The report also records the sequence of events and the relevant measurable change. It further documents confirmed chronology, official statements, named figures, prior confirmed context, market implications, credible reactions, and grounded next steps."
            ),
            entities=["event", "development"],
            raw_item_id=f"{event_id}-1",
        )
    ]
    if ready:
        reports.append(
            EventReport(
                report_id=f"{event_id}-2",
                source="Source Two",
                source_id="source-two",
                source_authority=0.8,
                headline=f"{event_id} receives independent confirmation in 2026",
                url=f"https://source-two.example/{event_id}",
                published_at="2026-09-22T00:01:00+00:00",
                retrieved_at="2026-09-22T00:01:00+00:00",
                description=(
                    "A second publication confirmed the development and reported 42% "
                    "growth, adding independent factual material, useful context, additional "
                    "timing, related evidence, clarification about the reported outcome, market "
                    "conditions, responsible participants, independently verifiable background, and "
                    "the latest documented status for comparison. Additional independently verified chronology, official comments, figures, prior context, implications, reactions, and next-step reporting are included for publishable depth."
                ),
                entities=["event", "development"],
                raw_item_id=f"{event_id}-2",
            )
        )
    return NewsEvent(
        event_id=event_id,
        canonical_title=f"{event_id} material development",
        topic="test",
        reports=reports,
    )


def test_canonical_v5_make_filters_and_backfills_readiness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = _event("evt-bad", ready=False)
    ready = [_event(f"evt-ready-{index}", ready=True) for index in range(1, 6)]
    events = [bad, *ready]

    class FakeCollector:
        def __init__(self, *args, **kwargs):
            pass

        def begin_run(self):
            pass

        def cache_stats(self):
            return {"cached_feeds": 0}

        def collect(self, sources):
            return events, SimpleNamespace(
                cache_hits=0,
                cache_misses=0,
                sources_attempted=0,
                sources_succeeded=0,
                sources_failed=0,
                raw_items_collected=0,
                errors=[],
            )

    class FakeFreshness:
        def __init__(self, config):
            pass

        def check(self, item):
            return SimpleNamespace(accepted=True)

    class FakeNiche:
        def classify(self, item):
            return RelevanceDecision.KEEP, "test", 1.0

    class FakeDeduper:
        def dedupe_batch(self, items):
            return items, {}

    class FakeClusterer:
        def __init__(self, config):
            pass

        def cluster_batch(self, items):
            return items

    class FakeIntelligence:
        def analyze(self, event):
            score = 100.0 if event.event_id == "evt-bad" else 10.0
            return SimpleNamespace(
                breaking=SimpleNamespace(score=score),
                momentum=SimpleNamespace(score=0.0),
                novelty=SimpleNamespace(score=0.0),
            )

    runtime = CanonicalV5Integration.__new__(CanonicalV5Integration)
    runtime.event_store = MagicMock()
    runtime.source_registry = MagicMock()
    runtime.source_registry.get_enabled.return_value = ["mock-source"]
    runtime.telegram_store = V5TelegramStore()
    runtime.client = MagicMock()
    runtime.client.send_message.side_effect = lambda **kwargs: {"ok": True, **kwargs}
    runtime.config = SimpleNamespace(test_chat_id="mock-chat")

    with patch("newsagent_v2.control.make_v5_bridge.CollectorV2", FakeCollector), \
         patch("newsagent_v2.control.make_v5_bridge.FreshnessEngine", FakeFreshness), \
         patch("newsagent_v2.control.make_v5_bridge.NicheFilter", FakeNiche), \
         patch("newsagent_v2.control.make_v5_bridge.DeduplicationEngine", FakeDeduper), \
         patch("newsagent_v2.control.make_v5_bridge.EventClusterer", FakeClusterer), \
         patch("newsagent_v2.control.make_v5_bridge.IntelligenceEngine", FakeIntelligence), \
         patch("newsagent_v2.control.make_v5_bridge.expand_sources_for_event", return_value=ExpansionResult(
             candidates_considered=0,
             sources_matched=0,
             sources_added=[],
             primary_sources_retained=0,
             independent_domains=[],
             failed_sources=[],
             diagnostics={},
         )):
        selected = runtime.run_discovery()

    assert [event.event_id for event in selected] == [
        f"evt-ready-{index}" for index in range(1, 6)
    ]
    sent_texts = [call.kwargs["text"] for call in runtime.client.send_message.call_args_list]
    assert len(sent_texts) == 5
    assert all("evt-bad" not in text for text in sent_texts)
    assert all(f"evt-ready-{index}" in text for index, text in enumerate(sent_texts, 1))

    audit = (tmp_path / "output" / "article_readiness.json").read_text(encoding="utf-8")
    assert '"evt-bad"' in audit
    assert '"eligible": false' in audit
    assert all(f'"evt-ready-{index}"' in audit for index in range(1, 6))


def test_canonical_v5_expands_thin_candidates_and_skips_exhausted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exhausted = _event("evt-exhausted", ready=False)
    thin = [_event(f"evt-thin-{index}", ready=False) for index in range(1, 7)]
    events = [exhausted, *thin]

    class FakeCollector:
        def __init__(self, *args, **kwargs):
            pass

        def begin_run(self):
            pass

        def cache_stats(self):
            return {"cached_feeds": 0}

        def collect(self, sources):
            return events, SimpleNamespace(
                cache_hits=0,
                cache_misses=0,
                sources_attempted=0,
                sources_succeeded=0,
                sources_failed=0,
                raw_items_collected=0,
                errors=[],
            )

    class FakeFreshness:
        def __init__(self, config):
            pass

        def check(self, item):
            return SimpleNamespace(accepted=True)

    class FakeNiche:
        def classify(self, item):
            return RelevanceDecision.KEEP, "test", 1.0

    class FakeDeduper:
        def dedupe_batch(self, items):
            return items, {}

    class FakeClusterer:
        def __init__(self, config):
            pass

        def cluster_batch(self, items):
            return items

    class FakeIntelligence:
        def analyze(self, event):
            return SimpleNamespace(
                breaking=SimpleNamespace(score=100.0 if event.event_id == "evt-exhausted" else 10.0),
                momentum=SimpleNamespace(score=0.0),
                novelty=SimpleNamespace(score=0.0),
            )

    def expand(event, **kwargs):
        if event["event_id"] == "evt-exhausted":
            rows = []
        else:
            rows = [
                {
                    "source": "Source Two",
                    "source_id": "source-two",
                    "source_authority": 0.8,
                    "url": f"https://source-two.example/{event['event_id']}",
                    "title": f"{event['event_id']} receives independent confirmation in 2026",
                    "published": "2026-09-22T00:01:00+00:00",
                    "summary": (
                        "A second publication confirmed the development and reported 42% growth, "
                        "adding independent factual material, useful context, timing, related evidence, "
                        "clarification, market conditions, participants, background, documented status, confirmed chronology, official statements, named figures, prior context, implications, credible reactions, and grounded next steps for publication."
                    ),
                },
            ]
        return ExpansionResult(
            candidates_considered=len(rows),
            sources_matched=len(rows),
            sources_added=rows,
            primary_sources_retained=0,
            independent_domains=["source-two.example"] if rows else [],
            failed_sources=[],
            diagnostics={"mock": True},
        )

    runtime = CanonicalV5Integration.__new__(CanonicalV5Integration)
    runtime.event_store = MagicMock()
    runtime.source_registry = MagicMock()
    runtime.telegram_store = V5TelegramStore()
    runtime.client = MagicMock()
    runtime.client.send_message.side_effect = lambda **kwargs: {"ok": True, **kwargs}
    runtime.config = SimpleNamespace(test_chat_id="mock-chat")

    with patch("newsagent_v2.control.make_v5_bridge.CollectorV2", FakeCollector), \
         patch("newsagent_v2.control.make_v5_bridge.FreshnessEngine", FakeFreshness), \
         patch("newsagent_v2.control.make_v5_bridge.NicheFilter", FakeNiche), \
         patch("newsagent_v2.control.make_v5_bridge.DeduplicationEngine", FakeDeduper), \
         patch("newsagent_v2.control.make_v5_bridge.EventClusterer", FakeClusterer), \
         patch("newsagent_v2.control.make_v5_bridge.IntelligenceEngine", FakeIntelligence), \
         patch("newsagent_v2.control.make_v5_bridge.expand_sources_for_event", side_effect=expand) as expansion_mock:
        selected = runtime.run_discovery()

    assert [event.event_id for event in selected] == [f"evt-thin-{index}" for index in range(1, 6)]
    assert expansion_mock.call_count >= 6
    assert runtime.client.send_message.call_count == 5

    diagnostics = (tmp_path / "output" / "source_expansion_readiness.json").read_text(encoding="utf-8")
    assert '"initial_source_count": 1' in diagnostics
    assert '"final_status": "FAIL"' in diagnostics
    assert '"final_status": "PASS"' in diagnostics