"""Tests for V5 Discovery Backbone.

Tests cover:
- Source registry
- Normalization
- Freshness filtering
- Niche filtering
- Deduplication
- Event clustering
- Intelligence engine
- State machine
- Cost boundaries
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.deduplicator import DeduplicationEngine
from newsagent_v2.discovery.event_clusterer import ClusterConfig, EventClusterer, EventReport, NewsEvent
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.freshness import FreshnessConfig, FreshnessEngine
from newsagent_v2.discovery.niche_filter import NicheConfig, NicheFilter, RelevanceDecision
from newsagent_v2.discovery.normalizer import NewsNormalizer, canonicalize_url, clean_text, normalize_title
from newsagent_v2.discovery.raw_news_item import RawNewsItem
from newsagent_v2.discovery.source_registry import SourceMetadata, SourceRegistry, SourceType
from newsagent_v2.intelligence.breaking_detector import BreakingDetector
from newsagent_v2.intelligence.engine import IntelligenceEngine
from newsagent_v2.intelligence.evolution import EvolutionAnalyzer
from newsagent_v2.intelligence.impact import ImpactAnalyzer
from newsagent_v2.intelligence.momentum import MomentumAnalyzer
from newsagent_v2.intelligence.novelty import NoveltyAnalyzer
from newsagent_v2.telegram_v5.callbacks import CallbackHandler
from newsagent_v2.telegram_v5.state_machine import EventState, StoryStateMachine
from newsagent_v2.v5_telemetry.cost_ledger import CostLedger


class TestNormalization:
    """Test normalization engine."""

    def test_clean_text_basic(self):
        """Test basic text cleaning."""
        assert clean_text("hello world") == "hello world"
        assert clean_text("  hello   world  ") == "hello world"
        assert clean_text("") == ""
        assert clean_text(None) == ""

    def test_clean_text_html(self):
        """Test HTML tag removal."""
        assert clean_text("<p>hello</p>") == "hello"
        assert clean_text("<p>hello <b>world</b></p>") == "hello world"

    def test_canonicalize_url_tracks_params(self):
        """Test URL canonicalization removes tracking params."""
        url = "https://example.com/article?utm_source=youtube&utm_medium=social"
        result = canonicalize_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "https://example.com/article" in result

    def test_normalize_title_stop_words(self):
        """Test title normalization removes stop words."""
        title = "the bitcoin price rises in the market"
        result = normalize_title(title)
        # 'in' is removed as a stop word
        words = result.split()
        assert "the" not in words
        assert "in" not in words
        assert "bitcoin" in words

    def test_normalizer_creates_raw_news_item(self):
        """Test NewsNormalizer creates RawNewsItem."""
        normalizer = NewsNormalizer()
        raw = {
            "title": "Bitcoin Price Surges",
            "link": "https://example.com/btc?utm=track",
            "summary": "Bitcoin price goes up",
            "published": "Mon, 01 Jan 2024 12:00:00 GMT",
        }
        source_meta = {
            "name": "Test Source",
            "source_id": "test",
            "source_type": "newsroom",
            "role": "discovery",
            "authority": 0.5,
        }

        item = normalizer.normalize_item(raw, source_meta)
        assert isinstance(item, RawNewsItem)
        assert item.headline == "Bitcoin Price Surges"
        assert item.source == "Test Source"


class TestFreshnessEngine:
    """Test freshness filtering."""

    def test_reject_old_items(self):
        """Test rejection of old items."""
        config = FreshnessConfig(max_age_hours=24.0, require_timestamp=False)
        engine = FreshnessEngine(config)

        # Create old item using RSS-compatible date format
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48))
        # Use RFC 2822 format which is what RSS feeds use
        from email.utils import format_datetime
        published_str = format_datetime(old_time.replace(tzinfo=timezone.utc))

        item = RawNewsItem(
            headline="Old news",
            canonical_url="https://example.com/old",
            fingerprint="abc123",
            published_at=published_str,
        )

        result = engine.check(item)
        assert not result
        # Check for the "too_old" reason
        assert "too_old" in result.reason or "old" in result.reason.lower()

    def test_accept_fresh_items(self):
        """Test acceptance of fresh items."""
        config = FreshnessConfig(max_age_hours=24.0)
        engine = FreshnessEngine(config)

        fresh_time = datetime.now(timezone.utc) - timedelta(hours=2)
        item = RawNewsItem(
            headline="Fresh news",
            canonical_url="https://example.com/fresh",
            fingerprint="abc123",
            published_at=fresh_time.isoformat(),
        )

        result = engine.check(item)
        assert result

    def test_reject_duplicate_urls(self):
        """Test rejection of duplicate URLs."""
        config = FreshnessConfig()
        engine = FreshnessEngine(config)

        item1 = RawNewsItem(
            headline="News",
            canonical_url="https://example.com/article",
            fingerprint="abc",
        )
        item2 = RawNewsItem(
            headline="Same News",
            canonical_url="https://example.com/article",
            fingerprint="def",
        )

        assert engine.check(item1)
        engine.mark_processed(item1)

        result = engine.check(item2)
        assert not result
        assert result.reason == "duplicate_url"


class TestNicheFilter:
    """Test crypto niche filter."""

    def test_keep_strong_crypto_signals(self):
        """Test keeping items with strong crypto signals."""
        niche = NicheFilter()

        # Strong signal
        item = RawNewsItem(
            headline="Bitcoin ETF Approves",
            canonical_url="https://example.com/btc",
            entities=["bitcoin"],
        )

        decision, reason, confidence = niche.classify(item)
        assert decision == RelevanceDecision.KEEP
        assert confidence > 0.8

    def test_reject_irrelevant_items(self):
        """Test rejecting irrelevant items."""
        niche = NicheFilter()

        item = RawNewsItem(
            headline="Apple announces new iPhone",
            canonical_url="https://example.com/apple",
            entities=["apple"],
        )

        decision, reason, _ = niche.classify(item)
        assert decision == RelevanceDecision.REJECT

    def test_reject_generic_roundups(self):
        """Test rejecting generic roundups."""
        niche = NicheFilter()

        item = RawNewsItem(
            headline="what happened in crypto today",
            canonical_url="https://example.com/roundup",
        )

        decision, reason, _ = niche.classify(item)
        assert decision == RelevanceDecision.REJECT
        assert "generic_roundup" in reason

    def test_reject_blocked_content(self):
        """Test rejecting sponsored/press release."""
        niche = NicheFilter()

        item = RawNewsItem(
            headline="Sponsored: Best Crypto Deals",
            canonical_url="https://example.com/sponsored",
        )

        decision, reason, _ = niche.classify(item)
        assert decision == RelevanceDecision.REJECT
        assert "blocked" in reason or "sponsored" in reason


class TestDeduplicationEngine:
    """Test deduplication engine."""

    def test_detect_exact_url_duplicates(self):
        """Test exact URL duplicate detection."""
        engine = DeduplicationEngine()

        item1 = RawNewsItem(
            headline="Bitcoin Price Up",
            canonical_url="https://coindesk.com/btc-up",
            fingerprint="fp1",
        )
        item2 = RawNewsItem(
            headline="Same Article Different Headline",
            canonical_url="https://coindesk.com/btc-up",
            fingerprint="fp2",
        )

        result1 = engine.check(item1)
        assert result1  # First is unique

        result2 = engine.check(item2)
        assert not result2  # Second is duplicate
        assert result2.duplicate_type == "exact"

    def test_detect_event_duplicates(self):
        """Test event-level duplicate detection."""
        engine = DeduplicationEngine()

        item1 = RawNewsItem(
            headline="Bitcoin ETF Approved by SEC",
            canonical_url="https://source1.com/etf",
            fingerprint="fp1",
            entities=["bitcoin", "etf", "sec"],
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        item2 = RawNewsItem(
            headline="SEC Approves Bitcoin ETF",
            canonical_url="https://source2.com/etf",
            fingerprint="fp2",
            entities=["bitcoin", "etf", "sec"],
            published_at=datetime.now(timezone.utc).isoformat(),
        )

        result1 = engine.check(item1)
        assert result1

        result2 = engine.check(item2)
        assert not result2
        assert result2.duplicate_type == "event"

    def test_dedupe_batch(self):
        """Test batch deduplication."""
        engine = DeduplicationEngine()

        # Use very distinct headlines/different items to avoid event-level matching
        headlines = [
            "Bitcoin ETF approved by SEC in landmark decision",
            "Ethereum price surges following network upgrade",
            "Coinbase announces new custody services",
            "DeFi protocol experiences security vulnerability",
            "Major bank to offer cryptocurrency trading",
            "Bitcoin ETF approved by SEC in landmark decision",  # Duplicate of item 0
            "Tether launches dollar-backed stablecoin",
            "SEC proposes new crypto regulation framework",
            "Mining difficulty reaches all-time high",
            "Major exchange adds new support for altcoins",
        ]

        items = [
            RawNewsItem(
                headline=headlines[i],
                canonical_url=f"https://example.com/{i}",
                fingerprint=f"unique_fp_{i}",
            )
            for i in range(10)
        ]

        # Mark item 5 as duplicate of item 0 (same URL and fingerprint)
        items[5].canonical_url = items[0].canonical_url
        items[5].fingerprint = items[0].fingerprint

        unique, duplicates = engine.dedupe_batch(items)
        # Item 0 is kept, item 5 is duplicate (based on URL and fingerprint)
        # So we should have 9 unique items and 1 duplicate
        assert len(unique) == 9, f"Expected 9 unique, got {len(unique)}: {[u.headline for u in unique]}"
        assert len(duplicates) == 1, f"Expected 1 duplicate, got {len(duplicates)}"


class TestEventClusterer:
    """Test event clusterer."""

    def test_create_new_event(self):
        """Test creation of new event."""
        clusterer = EventClusterer()

        item = RawNewsItem(
            headline="Bitcoin ETF Approved",
            canonical_url="https://example.com/btc-etf",
            entities=["bitcoin", "etf"],
            keywords=["bitcoin", "etf", "approved"],
            topics=["etf"],
            published_at=datetime.now(timezone.utc).isoformat(),
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            source="Test Source",
            source_id="test",
            source_authority=0.5,
        )
        item.compute_fingerprint()

        event = clusterer.process_item(item)
        assert isinstance(event, NewsEvent)
        assert len(event.reports) == 1

    def test_merge_similar_items_to_event(self):
        """Test merging similar items into existing event."""
        clusterer = EventClusterer()

        now = datetime.now(timezone.utc)

        # First report
        item1 = RawNewsItem(
            headline="Bitcoin ETF Approved by SEC",
            canonical_url="https://coindesk.com/etf",
            entities=["bitcoin", "etf", "sec"],
            keywords=["bitcoin", "etf", "approved", "sec"],
            topics=["etf"],
            published_at=now.isoformat(),
            retrieved_at=now.isoformat(),
            source="CoinDesk",
            source_id="coindesk",
            source_authority=0.8,
        )
        item1.compute_fingerprint()

        event1 = clusterer.process_item(item1)

        # Second report - similar, different source
        item2 = RawNewsItem(
            headline="SEC Approves Bitcoin ETF Application",
            canonical_url="https://theblock.com/etf",
            entities=["bitcoin", "etf", "sec"],
            keywords=["sec", "approves", "bitcoin", "etf"],
            topics=["etf"],
            published_at=now.isoformat(),
            retrieved_at=now.isoformat(),
            source="The Block",
            source_id="theblock",
            source_authority=0.8,
        )
        item2.compute_fingerprint()

        event2 = clusterer.process_item(item2)

        # Should be same event
        assert event1.event_id == event2.event_id
        assert len(event1.reports) == 2

    def test_different_items_create_separate_events(self):
        """Test that different items create separate events."""
        clusterer = EventClusterer()

        now = datetime.now(timezone.utc)

        item1 = RawNewsItem(
            headline="Bitcoin ETF Approved",
            canonical_url="https://example.com/btc",
            entities=["bitcoin"],
            topics=["etf"],
            published_at=now.isoformat(),
            retrieved_at=now.isoformat(),
            source="Source A",
            source_id="a",
            source_authority=0.5,
        )
        item1.compute_fingerprint()

        item2 = RawNewsItem(
            headline="Ethereum Price Drops",
            canonical_url="https://example.com/eth",
            entities=["ethereum"],
            topics=["market"],
            published_at=(now + timedelta(hours=12)).isoformat(),
            retrieved_at=(now + timedelta(hours=12)).isoformat(),
            source="Source B",
            source_id="b",
            source_authority=0.5,
        )
        item2.compute_fingerprint()

        event1 = clusterer.process_item(item1)
        event2 = clusterer.process_item(item2)

        # Should be different events
        assert event1.event_id != event2.event_id


class TestIntelligenceEngine:
    """Test intelligence components."""

    def test_momentum_analyzer_scoring(self):
        """Test momentum scoring."""
        analyzer = MomentumAnalyzer()

        event = NewsEvent(
            event_id="test-1",
            canonical_title="Test Event",
            entities=frozenset(["bitcoin"]),
            reports=[
                EventReport(
                    report_id="r1",
                    source="Source1",
                    source_id="s1",
                    source_authority=0.8,
                    headline="Headline",
                    url="https://example.com",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    description="Desc",
                    entities=["bitcoin"],
                    raw_item_id="r1",
                )
            ],
        )

        score = analyzer.analyze(event)
        assert score.level in ["LOW", "STABLE", "RISING", "HIGH"]
        assert score.score >= 0
        assert "source_count" in score.factors

    def test_evolution_analyzer(self):
        """Test evolution analyzer."""
        analyzer = EvolutionAnalyzer()

        event = NewsEvent(
            event_id="test-1",
            canonical_title="Test",
        )

        evo = analyzer.analyze(event)
        assert isinstance(evo.developing_story, bool)

    def test_novelty_analyzer_detects_repeat(self):
        """Test novelty detects repeat stories."""
        analyzer = NoveltyAnalyzer()

        seen_ids = {"test-1"}

        event = NewsEvent(
            event_id="test-1",
            canonical_title="Test",
        )

        result = analyzer.analyze(event, seen_ids)
        assert result.is_repeat

    def test_impact_analyzer_detects_areas(self):
        """Test impact analysis."""
        analyzer = ImpactAnalyzer()

        event = NewsEvent(
            event_id="test-1",
            canonical_title="Bitcoin ETF Approves",
            entities=frozenset(["bitcoin", "etf"]),
            reports=[
                EventReport(
                    report_id="r1",
                    source="Source1",
                    source_id="s1",
                    source_authority=0.8,
                    headline="Bitcoin ETF Approves",
                    url="https://example.com",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    description="Bitcoin ETF news",
                    entities=["bitcoin"],
                    raw_item_id="r1",
                )
            ],
        )

        result = analyzer.analyze(event)
        assert "Bitcoin" in result.affected_areas or "Bitcoin" == result.primary_area

    def test_breaking_detector(self):
        """Test breaking detection."""
        detector = BreakingDetector()

        now = datetime.now(timezone.utc)

        event = NewsEvent(
            event_id="test-1",
            canonical_title="Breaking News",
            reports=[
                EventReport(
                    report_id="r1",
                    source="Regulator",
                    source_id="reg",
                    source_authority=1.0,
                    headline="Breaking News",
                    url="https://example.com",
                    published_at=now.isoformat(),
                    retrieved_at=now.isoformat(),
                    description="Breaking",
                    entities=["bitcoin"],
                    raw_item_id="r1",
                ),
                EventReport(
                    report_id="r2",
                    source="News1",
                    source_id="n1",
                    source_authority=0.8,
                    headline="Breaking News",
                    url="https://example2.com",
                    published_at=(now + timedelta(minutes=30)).isoformat(),
                    retrieved_at=(now + timedelta(minutes=30)).isoformat(),
                    description="Breaking",
                    entities=["bitcoin"],
                    raw_item_id="r2",
                ),
                EventReport(
                    report_id="r3",
                    source="News2",
                    source_id="n2",
                    source_authority=0.8,
                    headline="Breaking News",
                    url="https://example3.com",
                    published_at=(now + timedelta(hours=1)).isoformat(),
                    retrieved_at=(now + timedelta(hours=1)).isoformat(),
                    description="Breaking",
                    entities=["bitcoin"],
                    raw_item_id="r3",
                ),
            ],
            first_seen=now.isoformat(),
            last_seen=now.isoformat(),
        )

        result = detector.analyze(event, now)
        assert isinstance(result.is_breaking, bool)


class TestCostLedger:
    """Test cost tracking."""

    def test_zero_discovery_cost(self):
        """Test that discovery cost is zero."""
        ledger = CostLedger()

        verified, msg = ledger.verify_zero_discovery_cost()
        assert verified
        assert "â‚¹0" in msg

    def test_no_writer_calls_in_discovery(self):
        """Test no writer calls recorded."""
        ledger = CostLedger()

        assert ledger.get_report().writer_calls == 0

    def test_no_image_calls_in_discovery(self):
        """Test no image calls recorded."""
        ledger = CostLedger()

        assert ledger.get_report().image_network_calls == 0


class TestStateMachine:
    """Test state machine."""

    def test_valid_transitions_allowed(self):
        """Test valid state transitions."""
        sm = StoryStateMachine()

        result = sm.validate_transition(
            "evt-1",
            EventState.DISCOVERED.value,
            EventState.AVAILABLE_IN_NEWS_DESK.value,
        )
        assert result.success

    def test_invalid_transitions_rejected(self):
        """Test invalid state transitions are rejected."""
        sm = StoryStateMachine()

        result = sm.validate_transition(
            "evt-1",
            EventState.DISCOVERED.value,
            EventState.PUBLISHED.value,
        )
        assert not result.success

    def test_duplicate_run_story_protection(self):
        """Test duplicate RUN STORY is blocked."""
        sm = StoryStateMachine()

        # Mark operation active
        assert sm.mark_operation_active("evt-1", EventState.SELECTED.value)

        # Second attempt should fail
        assert not sm.mark_operation_active("evt-1", EventState.SELECTED.value)

        # Complete operation
        sm.mark_operation_complete("evt-1", EventState.SELECTED.value)

        # Now can start again
        assert sm.mark_operation_active("evt-1", EventState.SELECTED.value)


class TestEventStore:
    """Test event store persistence."""

    def test_save_and_load(self, tmp_path: Path):
        """Test saving and loading events."""
        store = EventStore(root=tmp_path)

        event = NewsEvent(
            event_id="test-evt-1",
            canonical_title="Test Event",
            entities=frozenset(["bitcoin"]),
        )

        path = store.save(event)
        assert path.is_file()

        loaded = store.get("test-evt-1")
        assert loaded is not None
        assert loaded.canonical_title == "Test Event"

    def test_merge_or_create(self, tmp_path: Path):
        """Test merge or create functionality."""
        store = EventStore(root=tmp_path)

        event1 = NewsEvent(
            event_id="test-evt-1",
            canonical_title="Test",
        )

        # First save
        store.merge_or_create(event1)

        # Second save (should merge)
        event2 = NewsEvent(
            event_id="test-evt-1",
            canonical_title="Test Updated",
        )

        merged = store.merge_or_create(event2)
        assert merged.canonical_title == "Test Updated"

    def test_mark_followed(self, tmp_path: Path):
        """Test marking events as followed."""
        store = EventStore(root=tmp_path)

        event = NewsEvent(event_id="test-evt-1")
        store.save(event)

        updated = store.mark_followed("test-evt-1", True)
        assert updated is not None
        assert updated.followed

    def test_mark_ignored(self, tmp_path: Path):
        """Test marking events as ignored."""
        store = EventStore(root=tmp_path)

        event = NewsEvent(event_id="test-evt-1")
        store.save(event)

        updated = store.mark_ignored("test-evt-1", True)
        assert updated is not None
        assert updated.ignored


class TestCallbackHandler:
    """Test callback handler."""

    def test_callback_parsing(self, tmp_path: Path):
        """Test callback data parsing."""
        store = EventStore(root=tmp_path)
        handler = CallbackHandler(
            event_store=store,
            state_machine=StoryStateMachine(),
        )

        _, action, event_id = handler.parse_callback("v5:run:evt-123")
        assert action == "run"
        assert event_id == "evt-123"

    def test_run_story_event_not_found(self, tmp_path: Path):
        """Test RUN STORY with non-existent event."""
        store = EventStore(root=tmp_path)
        handler = CallbackHandler(
            event_store=store,
            state_machine=StoryStateMachine(),
        )

        result = handler.handle_run_story("nonexistent")
        assert not result["ok"]
        assert result["error"] == "event_not_found"

    def test_follow_event_not_found(self, tmp_path: Path):
        """Test FOLLOW with non-existent event."""
        store = EventStore(root=tmp_path)
        handler = CallbackHandler(
            event_store=store,
            state_machine=StoryStateMachine(),
        )

        result = handler.handle_follow("nonexistent")
        assert not result["ok"]
        assert result["error"] == "event_not_found"


class TestSourceRegistry:
    """Test source registry."""

    def test_default_sources_loaded(self):
        """Test default sources are loaded."""
        registry = SourceRegistry()

        enabled = registry.get_enabled()
        assert len(enabled) > 0

        coindesk = registry.get("coindesk")
        assert coindesk is not None
        assert coindesk.name == "CoinDesk"

    def test_get_by_type(self):
        """Test filtering sources by type."""
        registry = SourceRegistry()

        regulators = registry.get_by_type("official_regulator")
        assert len(regulators) > 0
        for s in regulators:
            assert "cftc" in s.source_id or "fca" in s.source_id

    def test_record_success_and_failure(self):
        """Test health tracking."""
        registry = SourceRegistry()

        registry.record_success("coindesk")
        source = registry.get("coindesk")
        assert source.last_success is not None
        assert source.failure_count == 0

        registry.record_failure("coindesk", "timeout")
        source = registry.get("coindesk")
        assert source.failure_count == 1
        assert source.error_reason == "timeout"


class TestV5Integration:
    """Integration tests for V5 components."""

    def test_end_to_end_discovery_pipeline(self, tmp_path: Path):
        """Test complete discovery pipeline with mocked data."""
        from collections import namedtuple

        # Create mock source registry
        registry = SourceRegistry()

        # Override with single mock source
        mock_source = SourceMetadata(
            source_id="mock",
            name="Mock Source",
            source_type="rss",
            url="https://mock.example.com/feed",
            enabled=True,
        )
        registry._sources = {"mock": mock_source}

        # Create stores
        store = EventStore(root=tmp_path)

        # Create pipeline components
        normalizer = NewsNormalizer()
        freshness = FreshnessEngine()
        niche = NicheFilter()
        dedup = DeduplicationEngine()
        clusterer = EventClusterer()

        # Simulate raw items from RSS parsing
        raw_items = [
            {
                "title": "Bitcoin ETF Approved by SEC",
                "link": "https://example.com/1",
                "summary": "The SEC approves Bitcoin ETF",
                "published": datetime.now(timezone.utc).isoformat(),
            },
            {
                "title": "Ethereum Price Surges",
                "link": "https://example.com/2",
                "summary": "ETH price goes up",
                "published": datetime.now(timezone.utc).isoformat(),
            },
            {
                "title": "Apple announces iPhone",  # Should be filtered
                "link": "https://example.com/3",
                "summary": "Apple news",
                "published": datetime.now(timezone.utc).isoformat(),
            },
        ]

        # Normalize
        source_meta = {
            "name": "Mock Source",
            "source_id": "mock",
            "source_type": "rss",
            "role": "discovery",
            "authority": 0.5,
        }
        items = [normalizer.normalize_item(r, source_meta) for r in raw_items]

        # Freshness filter
        fresh_items, _ = freshness.filter_batch(items)
        assert len(fresh_items) == 3

        # Niche filter
        kept, conditional, rejected = niche.filter_batch(fresh_items)
        # Bitcoin and Ethereum should be kept, Apple rejected
        assert len(kept) + len(conditional) >= 2
        assert len(rejected) >= 1

        relevant = kept + conditional

        # Deduplication
        unique, dupes = dedup.dedupe_batch(relevant)
        assert len(dupes) == 0  # All URLs different

        # Clustering
        events = clusterer.cluster_batch(unique)
        assert len(events) > 0

        # Persistence
        for event in events:
            store.merge_or_create(event)

        # Verify persistence
        all_events = store.get_all()
        assert len(all_events) > 0

        # Intelligence
        intel = IntelligenceEngine()
        for event in all_events:
            event_intel = intel.analyze(event)
            assert event_intel.momentum.level in ["LOW", "STABLE", "RISING", "HIGH"]

    def test_event_persistence_survives_restart(self, tmp_path: Path):
        """Test that events persist across store recreation."""
        store1 = EventStore(root=tmp_path)

        event = NewsEvent(
            event_id="persist-test-1",
            canonical_title="Persistence Test",
        )
        store1.save(event)

        # Create new store (simulating restart)
        store2 = EventStore(root=tmp_path)

        loaded = store2.get("persist-test-1")
        assert loaded is not None
        assert loaded.canonical_title == "Persistence Test"


