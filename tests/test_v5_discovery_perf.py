"""Performance/regression tests for V5 discovery Top-5 optimizations.

Proves (mocked slow/dead feeds; no live /make, no Telegram/WP):
- bounded concurrency + timeouts do not hang forever
- each feed URL fetched at most once per discovery run (run-scoped cache)
- early-stop when 5 candidates meet 600+ capacity
- quality/depth requirements still enforced (600+ not weakened)
"""

from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from newsagent_v2.discovery.collector_v2 import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_REQUEST_TIMEOUT,
    CollectorV2,
    SourceResult,
)
from newsagent_v2.discovery.raw_news_item import RawNewsItem
from newsagent_v2.discovery.source_registry import SourceMetadata
from newsagent_v2.v5_generation.depth_cost_helpers import (
    NORMAL_PASS_MIN,
    evidence_supports_publishable_depth,
)
from newsagent_v2.v5_generation.source_expansion_adapter import expand_sources_for_event
from newsagent_v2.article_readiness import INTENTIONAL_ARTICLE_WORDS


def _source(i: int, *, dead: bool = False) -> SourceMetadata:
    return SourceMetadata(
        source_id=f"feed_{i}",
        name=f"Feed {i}",
        source_type="crypto_publication",
        url=f"https://feeds.example/{i}.xml",
        enabled=True,
        authority=0.7,
        role="discovery",
        timeout_seconds=5,
        retry_limit=1,
    )


def _item(i: int) -> RawNewsItem:
    return RawNewsItem(
        source=f"Feed {i}",
        source_id=f"feed_{i}",
        source_type="crypto_publication",
        source_role="discovery",
        source_authority=0.7,
        headline=f"Bitcoin ETF Approval detail {i}",
        description=(
            "In 2026 the SEC said it approved the product after 12,400 comments. "
            "Previously the agency delayed the decision. The move could impact markets. "
            "Next steps include listing and surveillance sharing. Background filings mattered."
        ),
        canonical_url=f"https://news.example/story/{i}",
        published_at="2026-09-22T00:00:00+00:00",
        entities=["Bitcoin", "SEC"],
        topics=["regulatory"],
        keywords=["bitcoin", "sec", "etf"],
    )


class CountingCollector(CollectorV2):
    def __init__(self, *args, slow_ms: float = 30.0, hang_ids: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.network_fetches = 0
        self.urls: list[str] = []
        self.slow_ms = slow_ms
        self.hang_ids = hang_ids or set()
        self.max_observed_inflight = 0
        self._inflight = 0
        self._lock = __import__("threading").Lock()

    def _fetch_source_uncached(self, source: SourceMetadata):
        with self._lock:
            self._inflight += 1
            self.max_observed_inflight = max(self.max_observed_inflight, self._inflight)
            self.network_fetches += 1
            self.urls.append(source.url)
        try:
            if source.source_id in self.hang_ids:
                # Would hang forever without timeouts; sleep longer than request timeout budget.
                time.sleep(self.request_timeout + 3)
            else:
                time.sleep(self.slow_ms / 1000.0)
            if source.source_id in self.hang_ids:
                return [], SourceResult(
                    source_id=source.source_id,
                    success=False,
                    items_collected=0,
                    items_normalized=0,
                    error="simulated_hang_completed",
                    latency_ms=(self.request_timeout + 3) * 1000,
                )
            item = _item(int(source.source_id.split("_")[1]))
            return [item], SourceResult(
                source_id=source.source_id,
                success=True,
                items_collected=1,
                items_normalized=1,
                latency_ms=self.slow_ms,
                feed_entries=1,
            )
        finally:
            with self._lock:
                self._inflight -= 1


class TestCollectorCacheAndBounds(unittest.TestCase):
    def test_defaults_are_bounded_and_strict(self):
        self.assertLessEqual(DEFAULT_MAX_WORKERS, 16)
        self.assertGreaterEqual(DEFAULT_MAX_WORKERS, 4)
        self.assertLessEqual(DEFAULT_REQUEST_TIMEOUT, 15)
        self.assertGreaterEqual(DEFAULT_REQUEST_TIMEOUT, 5)

    def test_feed_fetched_at_most_once_per_run(self):
        sources = [_source(i) for i in range(12)]
        collector = CountingCollector(
            max_workers=4,
            request_timeout=5,
            max_retries=0,
            enable_run_cache=True,
            slow_ms=20.0,
        )
        collector.begin_run()
        items1, diag1 = collector.collect(sources)
        self.assertEqual(collector.network_fetches, 12)
        self.assertEqual(diag1.cache_misses, 12)
        self.assertEqual(diag1.cache_hits, 0)

        items2, diag2 = collector.collect(sources)
        # Second collect must reuse cache — zero additional network fetches.
        self.assertEqual(collector.network_fetches, 12)
        self.assertEqual(diag2.cache_hits, 12)
        self.assertEqual(diag2.cache_misses, 0)
        self.assertEqual(len(items1), len(items2))

    def test_expand_reuses_shared_collector_cache(self):
        sources = [_source(i) for i in range(10)]
        collector = CountingCollector(
            max_workers=4,
            request_timeout=5,
            max_retries=0,
            enable_run_cache=True,
            slow_ms=15.0,
        )
        collector.begin_run()
        collector.collect(sources)
        self.assertEqual(collector.network_fetches, 10)

        registry = MagicMock()
        registry.get_enabled.return_value = sources
        t0 = time.perf_counter()
        for e in range(6):
            expand_sources_for_event(
                event={
                    "event_id": f"e{e}",
                    "representative_title": "Bitcoin ETF Approval",
                    "canonical_title": "Bitcoin ETF Approval",
                },
                event_entities=["Bitcoin", "SEC"],
                event_topic="regulatory",
                event_reports=[{"url": f"https://seed.example/{e}", "title": "seed", "summary": "seed"}],
                source_registry=registry,
                collector=collector,
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(
            collector.network_fetches,
            10,
            "dimension-gap expansion must not rescan feeds when cache is shared",
        )
        # 6 expansions x 10 feeds x 15ms would be ~900ms+ without cache; with cache should be << 300ms.
        self.assertLess(elapsed_ms, 500.0)

    def test_concurrency_bounded(self):
        sources = [_source(i) for i in range(16)]
        collector = CountingCollector(
            max_workers=3,
            request_timeout=5,
            max_retries=0,
            enable_run_cache=True,
            slow_ms=40.0,
        )
        collector.begin_run()
        collector.collect(sources)
        self.assertLessEqual(collector.max_observed_inflight, 3)
        self.assertEqual(collector.network_fetches, 16)

    def test_timeouts_do_not_hang_forever(self):
        """A dead/hanging feed must not stall the whole collect indefinitely."""
        sources = [_source(i) for i in range(4)]
        collector = CountingCollector(
            max_workers=2,
            request_timeout=1,
            max_retries=0,
            enable_run_cache=True,
            slow_ms=10.0,
            hang_ids={"feed_1"},
        )
        collector.begin_run()
        t0 = time.perf_counter()
        # Patch uncached fetch path used by hang: override download to hang, but
        # our CountingCollector already sleeps timeout+3. Collect uses future.result
        # with bounded timeout so overall should finish well under "forever".
        items, diag = collector.collect(sources)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 12.0, f"collect hung too long: {elapsed}s")
        self.assertEqual(diag.sources_attempted, 4)


class TestEarlyStopAndDepthPreserved(unittest.TestCase):
    def test_capacity_target_still_600(self):
        self.assertEqual(INTENTIONAL_ARTICLE_WORDS, 600)
        self.assertEqual(NORMAL_PASS_MIN, 600)
        ok, _ = evidence_supports_publishable_depth(
            {
                "evidence_word_count": 200,
                "distinct_source_count": 2,
                "evidence_item_count": 2,
                "intended_article_words": 600,
            }
        )
        self.assertTrue(ok)
        bad, reasons = evidence_supports_publishable_depth(
            {
                "evidence_word_count": 20,
                "distinct_source_count": 1,
                "evidence_item_count": 1,
                "intended_article_words": 600,
            }
        )
        self.assertFalse(bad)
        self.assertTrue(reasons)

    def test_run_discovery_early_stops_at_five_ready(self):
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        from newsagent_v2.discovery.event_clusterer import EventReport, NewsEvent

        def make_event(i: int, rich: bool) -> NewsEvent:
            desc = (
                (
                    "In 2026 the SEC said it approved the product after 12,400 comments. "
                    "Previously the agency delayed the decision. The move could impact markets. "
                    "Next steps include listing and surveillance sharing. Background context included. "
                )
                * (6 if rich else 1)
            )
            return NewsEvent(
                event_id=f"evt-{i}",
                canonical_title=f"Bitcoin ETF story {i}",
                topic="regulatory",
                entities=["Bitcoin", "SEC"],
                reports=[
                    EventReport(
                        report_id=f"r{i}a",
                        source="SEC",
                        source_id="sec",
                        source_authority=1.0,
                        headline=f"Bitcoin ETF story {i}",
                        url=f"https://sec.example/{i}",
                        published_at="2026-09-22T00:00:00+00:00",
                        retrieved_at="2026-09-22T00:00:00+00:00",
                        description=desc,
                        entities=["Bitcoin", "SEC"],
                        raw_item_id=f"r{i}a",
                    ),
                    EventReport(
                        report_id=f"r{i}b",
                        source="Exchange",
                        source_id="ex",
                        source_authority=0.9,
                        headline=f"Listing timeline {i}",
                        url=f"https://exchange.example/{i}",
                        published_at="2026-09-22T00:00:00+00:00",
                        retrieved_at="2026-09-22T00:00:00+00:00",
                        description=desc,
                        entities=["Bitcoin"],
                        raw_item_id=f"r{i}b",
                    ),
                ],
            )

        events = [make_event(i, rich=True) for i in range(8)]
        # Insert a thin one early that should fail and be skipped via backfill.
        events.insert(2, make_event(99, rich=False))

        pipeline = V5DiscoveryPipeline.__new__(V5DiscoveryPipeline)
        pipeline.source_registry = MagicMock()
        pipeline.source_registry.get_enabled.return_value = [_source(0)]
        pipeline.max_workers = 2
        pipeline.event_store = MagicMock()
        pipeline.telegram_store = MagicMock()
        pipeline._source_expansion_diagnostics = {}
        pipeline._discovery_timings = {}
        pipeline._run_collector = None
        pipeline._ranked_events = []
        pipeline._discovery_llm_calls = 0
        pipeline._writer_calls = 0
        pipeline._image_calls = 0
        pipeline._research_calls = 0
        pipeline._cost_inr = 0.0

        expand_calls = {"n": 0}

        def fake_expand_until_ready(event, *, collector=None):
            expand_calls["n"] += 1
            # Treat rich descriptions as ready without network.
            ready = "12,400" in (event.reports[0].description or "") and len(
                event.reports[0].description or ""
            ) > 400
            return ready, {
                "event_id": event.event_id,
                "expansion_rounds": [],
                "readiness_attempts": [],
                "final_status": "PASS" if ready else "FAIL",
                "final_readiness": {"eligible": ready, "reasons": [], "metrics": {}},
                "final_reasons": [],
            }

        collector = CountingCollector(
            max_workers=2, request_timeout=2, max_retries=0, enable_run_cache=True, slow_ms=5.0
        )

        with patch.object(V5DiscoveryPipeline, "_expand_until_ready", autospec=True) as mocked:
            mocked.side_effect = lambda self, event, *, collector=None: fake_expand_until_ready(
                event, collector=collector
            )
            with patch("newsagent_v2.control.make_v5_bridge.CollectorV2", return_value=collector):
                with patch("newsagent_v2.control.make_v5_bridge.FreshnessEngine") as Fresh:
                    Fresh.return_value.check.side_effect = lambda item: MagicMock(accepted=True)
                    with patch("newsagent_v2.control.make_v5_bridge.NicheFilter") as Niche:
                        from newsagent_v2.discovery.niche_filter import RelevanceDecision

                        Niche.return_value.classify.return_value = (
                            RelevanceDecision.KEEP,
                            "ok",
                            1.0,
                        )
                        with patch("newsagent_v2.control.make_v5_bridge.DeduplicationEngine") as Ded:
                            Ded.return_value.dedupe_batch.side_effect = lambda items: (items, [])
                            with patch("newsagent_v2.control.make_v5_bridge.EventClusterer") as Clus:
                                Clus.return_value.cluster_batch.return_value = events
                                with patch("newsagent_v2.control.make_v5_bridge.IntelligenceEngine") as Intel:
                                    Intel.return_value.analyze.side_effect = lambda e: MagicMock(
                                        breaking=MagicMock(score=1.0),
                                        momentum=MagicMock(score=1.0),
                                        novelty=MagicMock(score=1.0),
                                    )
                                    with patch("newsagent_v2.control.make_v5_bridge.Path"):
                                        selected = V5DiscoveryPipeline.run_discovery(pipeline)

        self.assertEqual(len(selected), 5)
        # Should not expand all 9 events once 5 ready (thin one may consume one expand).
        self.assertLessEqual(expand_calls["n"], 6)
        self.assertGreaterEqual(expand_calls["n"], 5)


if __name__ == "__main__":
    unittest.main()
