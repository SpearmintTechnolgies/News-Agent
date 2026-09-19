"""V5 /make command implementation.

Complete discovery pipeline:
/make → Collector V2 → RawNewsItem → Normalization → Freshness → Niche
→ Deduplication → Event Clusterer → Persistent Event Store → Intelligence
→ Telegram News Desk
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.discovery.collector_v2 import CollectorV2, collect_sources
from newsagent_v2.discovery.deduplicator import DeduplicationEngine
from newsagent_v2.discovery.event_clusterer import EventClusterer, NewsEvent
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.freshness import FreshnessEngine, FreshnessConfig
from newsagent_v2.discovery.niche_filter import NicheFilter, NicheConfig
from newsagent_v2.discovery.normalizer import NewsNormalizer
from newsagent_v2.discovery.raw_news_item import RawNewsItem
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.intelligence.engine import IntelligenceEngine
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram_v5.callbacks import CallbackHandler
from newsagent_v2.telegram_v5.news_desk import NewsDesk, RenderedCard
from newsagent_v2.telegram_v5.state_machine import EventState, StoryStateMachine
from newsagent_v2.v5_telemetry.cost_ledger import CostLedger
from newsagent_v2.v5_telemetry.diagnostics import DiscoveryDiagnostics, V5Diagnostics

DEFAULT_V5_STORE_PATH = Path(__file__).resolve().parents[3] / "output" / "v5_events"


class V5MakePipeline:
    """V5 /make pipeline.

    ZERO paid model calls in discovery phase.
    All processing is deterministic code-first.
    """

    def __init__(
        self,
        source_registry: SourceRegistry | None = None,
        event_store: EventStore | None = None,
        store_path: Path | None = None,
        max_workers: int = 4,
        per_feed_limit: int = 30,
    ):
        # Core components
        self.source_registry = source_registry or SourceRegistry()
        store_path = store_path or DEFAULT_V5_STORE_PATH
        self.event_store = event_store or EventStore(root=store_path)

        # Pipeline engines
        self.collector = CollectorV2(
            max_workers=max_workers,
            per_feed_limit=per_feed_limit,
        )
        self.normalizer = NewsNormalizer()
        self.freshness = FreshnessEngine(
            config=FreshnessConfig(
                max_age_hours=48.0,
                require_timestamp=False,
            )
        )
        self.niche = NicheFilter(
            config=NicheConfig(strict_mode=False),
        )
        self.deduplicator = DeduplicationEngine()
        self.clusterer = EventClusterer()

        # Intelligence
        self.intelligence = IntelligenceEngine()

        # Telegram
        self.state_machine = StoryStateMachine()
        self.news_desk: NewsDesk | None = None
        self.callback_handler: CallbackHandler | None = None

        # Telemetry
        self.cost_ledger = CostLedger()
        self.diagnostics = V5Diagnostics()

        # Results
        self._events: list[NewsEvent] = []

    def _init_telegram(
        self,
        event_store: EventStore,
    ) -> None:
        """Initialize Telegram components."""
        self.news_desk = NewsDesk(
            event_store=event_store,
            intelligence=self.intelligence,
        )
        self.callback_handler = CallbackHandler(
            event_store=event_store,
            state_machine=self.state_machine,
        )

    def run_discovery(self) -> V5Diagnostics:
        """Run the complete V5 discovery pipeline.

        Returns:
            V5Diagnostics with complete metrics
        """
        start_time = time.perf_counter()
        discovery_diag = DiscoveryDiagnostics()
        discovery_diag.started_at = datetime.utcnow().isoformat()

        # 1. COLLECT from sources
        enabled_sources = self.source_registry.get_enabled()
        discovery_diag.sources_attempted = len(enabled_sources)

        raw_items, collector_diag = self.collector.collect(enabled_sources)
        discovery_diag.source_results = [
            {
                "source_id": r.source_id,
                "success": r.success,
                "items_collected": r.items_collected,
                "latency_ms": round(r.latency_ms, 2),
                "error": r.error,
            }
            for r in collector_diag.source_results
        ]
        discovery_diag.sources_succeeded = collector_diag.sources_succeeded
        discovery_diag.sources_failed = collector_diag.sources_failed
        discovery_diag.raw_reports = len(raw_items)
        discovery_diag.errors.extend(collector_diag.errors)

        # 2. NORMALIZE (already done in collector, but track)
        normalized_items = raw_items
        discovery_diag.normalized = len(normalized_items)

        # 3. FRESHNESS FILTER
        fresh_items, _ = self.freshness.filter_batch(normalized_items)
        discovery_diag.fresh = len(fresh_items)

        # 4. NICHE FILTER
        kept, conditional, rejected = self.niche.filter_batch(fresh_items)
        # Combine kept + conditional (conditional are still relevant)
        relevant_items = kept + conditional
        discovery_diag.niche_relevant = len(relevant_items)

        # 5. DEDUPLICATION
        unique_items, duplicates = self.deduplicator.dedupe_batch(relevant_items)
        discovery_diag.exact_duplicates_removed = len(duplicates)
        discovery_diag.after_dedupe = len(unique_items)

        # 6. EVENT CLUSTERING
        # First, load existing events
        existing_events = self.event_store.get_all()
        for event in existing_events:
            # Add to clusterer
            self.clusterer.events.append(event)

        # Process new items
        affected_events = self.clusterer.cluster_batch(unique_items)
        discovery_diag.meaningful_developments = sum(
            len(e.developments) for e in affected_events
        )

        # Count new vs updated
        existing_ids = {e.event_id for e in existing_events}
        new_events = [e for e in affected_events if e.event_id not in existing_ids]
        updated_events = [e for e in affected_events if e.event_id in existing_ids]

        discovery_diag.new_events_created = len(new_events)
        discovery_diag.existing_events_updated = len(updated_events)

        # 7. PERSIST EVENTS
        for event in affected_events:
            self.event_store.merge_or_create(event)

        # Get active (non-ignored) events
        active_events = self.event_store.get_active_events()
        self._events = active_events
        discovery_diag.events_presented = len(active_events)

        # Mark discovery complete
        discovery_diag.mark_complete()
        self.diagnostics.discovery = discovery_diag

        # Run intelligence on all events
        self.diagnostics.events_analyzed = len(active_events)

        # Count breaking signals
        breaking_count = 0
        for event in active_events:
            intel = self.intelligence.analyze(event)
            if intel.breaking.is_breaking:
                breaking_count += 1

        self.diagnostics.events_with_breaking = breaking_count
        self.diagnostics.events_followed = len(self.event_store.get_followed())
        self.diagnostics.events_ignored = len(self.event_store.get_ignored())

        # Runtime
        self.diagnostics.total_llm_calls = 0
        self.diagnostics.total_writer_calls = 0
        self.diagnostics.total_image_calls = 0
        self.diagnostics.total_estimated_cost = "₹0"

        # Verify cost boundary
        verified, msg = self.cost_ledger.verify_zero_discovery_cost()
        self.diagnostics.cost_boundary_verified = verified

        return self.diagnostics

    def render_cards(self, events: list[NewsEvent] | None = None) -> list[RenderedCard]:
        """Render Telegram cards for events."""
        if self.news_desk is None:
            raise RuntimeError("Telegram not initialized. Call init_telegram first.")

        events = events or self._events
        cards = self.news_desk.render_cards_batch(events)
        self.diagnostics.cards_rendered = len(cards)
        return cards

    def send_cards_to_telegram(
        self,
        cards: list[RenderedCard],
        client: TelegramTestClient,
        config: TelegramConfig,
    ) -> list[dict[str, Any]]:
        """Send rendered cards to Telegram."""
        results: list[dict[str, Any]] = []

        for card in cards:
            result = client.send_message(
                chat_id=config.test_chat_id,
                text=card.text,
                parse_mode=card.parse_mode,
                disable_web_page_preview=False,
            )
            results.append(result)

        self.diagnostics.cards_sent = len([r for r in results if r.get("ok")])
        return results

    def get_events(self) -> list[NewsEvent]:
        """Get discovered events."""
        return self._events

    def get_top_events(
        self,
        count: int = 5,
        offset: int = 0,
        exclude_ignored: bool = True,
    ) -> list[NewsEvent]:
        """Get top ranked events for display.

        Args:
            count: Number of events to return
            offset: Start from this index (for pagination)
            exclude_ignored: Skip ignored events

        Returns:
            List of events sorted by importance (breaking > momentum > recency)
        """
        events = self._events

        if exclude_ignored:
            events = [e for e in events if e.state != "IGNORED"]

        # Score events for ranking
        def score_event(e: NewsEvent) -> tuple:
            """Return score tuple (breaking, momentum_score, negative_age)."""
            intel = getattr(e, 'intelligence', None)
            if intel:
                breaking = 1 if getattr(intel, 'breaking_signal', None) and \
                               getattr(intel.breaking_signal, 'is_breaking', False) else 0
                momentum = getattr(intel, 'momentum_score', 0)
            else:
                breaking = 0
                momentum = getattr(e, 'momentum_score', 0)

            # Negative age so newer = higher
            try:
                from datetime import datetime
                age = (datetime.utcnow() - datetime.fromisoformat(e.first_seen.replace('Z', '+00:00').replace('+00:00', ''))).total_seconds()
            except:
                age = 999999

            return (breaking, momentum, -age)

        ranked = sorted(events, key=score_event, reverse=True)

        return ranked[offset:offset + count]

    def get_diagnostics(self) -> V5Diagnostics:
        """Get complete diagnostics."""
        return self.diagnostics

    def get_cost_report(self) -> dict[str, Any]:
        """Get cost report."""
        report = self.cost_ledger.get_report()
        return {
            "discovery_llm_calls": report.discovery_llm_calls,
            "research_llm_calls": report.research_llm_calls,
            "writer_calls": report.writer_calls,
            "image_network_calls": report.image_network_calls,
            "image_cost_inr": report.image_cost_inr,
            "estimated_total_inr": getattr(report, 'estimated_cost_inr', 0),
        }


def run_v5_make(
    source_registry: SourceRegistry | None = None,
    event_store: EventStore | None = None,
    telegram_client: TelegramTestClient | None = None,
    telegram_config: TelegramConfig | None = None,
    max_workers: int = 4,
    send_to_telegram: bool = False,
) -> dict[str, Any]:
    """Run V5 /make command.

    Returns:
        Complete result with diagnostics, events, costs
    """
    pipeline = V5MakePipeline(
        source_registry=source_registry,
        event_store=event_store,
        max_workers=max_workers,
    )

    # Run discovery
    diagnostics = pipeline.run_discovery()

    # Initialize Telegram if client provided
    if telegram_client and telegram_config:
        pipeline._init_telegram(pipeline.event_store)

        if send_to_telegram:
            # Render and send cards
            cards = pipeline.render_cards()
            pipeline.send_cards_to_telegram(
                cards=cards,
                client=telegram_client,
                config=telegram_config,
            )

    # Save diagnostics
    diag_path = diagnostics.save()

    return {
        "ok": True,
        "diagnostics": diagnostics.to_dict(),
        "events": [e.to_dict() for e in pipeline.get_events()],
        "cost": pipeline.get_cost_report(),
        "diagnostic_file": str(diag_path),
    }
