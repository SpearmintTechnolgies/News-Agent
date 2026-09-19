"""V5 /make bridge: Discovery-only, Top 5 → Telegram.

This module provides the V5 discovery pipeline that:
1. Runs Collector V2 (zero cost)
2. Ranks events by intelligence
3. Sends Top 5 to Telegram as separate cards
4. Supports SEE NEXT 5 pagination

RUN STORY only marks selection, does NOT invoke writer/image.
"""

from __future__ import annotations

import os
from time import perf_counter
from typing import Any, Callable

from pathlib import Path

from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.deduplicator import DeduplicationEngine
from newsagent_v2.discovery.event_clusterer import EventClusterer, NewsEvent, ClusterConfig
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.freshness import FreshnessEngine, FreshnessConfig
from newsagent_v2.intelligence.engine import IntelligenceEngine
from newsagent_v2.discovery.niche_filter import NicheFilter
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.telegram.v5_cards import render_card_from_event, seepage_keyboard


class V5DiscoveryPipeline:
    """V5 discovery pipeline: Collector → Rank → Send Top N.
    
    Zero paid model costs.
    """
    
    def __init__(
        self,
        event_store: EventStore | None = None,
        source_registry: SourceRegistry | None = None,
        telegram_store: V5TelegramStore | None = None,
        max_workers: int = 4,
    ) -> None:
        self.event_store = event_store or EventStore(root=Path("./data/events"))
        self.source_registry = source_registry or SourceRegistry()
        self.telegram_store = telegram_store or V5TelegramStore()
        self.max_workers = max_workers
        
        # Discovery cost tracking
        self._discovery_llm_calls = 0
        self._writer_calls = 0
        self._image_calls = 0
        self._research_calls = 0
        self._cost_inr = 0.0
        self._start_time = 0.0
        
        # Results
        self._ranked_events: list[NewsEvent] = []
    
    def run_discovery(self) -> list[NewsEvent]:
        """Run full V5 discovery pipeline.
        
        Returns ranked list of NewsEvents.
        """
        self._start_time = perf_counter()
        
        # 1. Collect from sources - get list of sources
        sources = self.source_registry.get_enabled()
        collector = CollectorV2()
        raw_items, diagnostics = collector.collect(sources)
        
        # 2. FreshnessEngine and NicheFilter expect RawNewsItem
        normalized = raw_items
        
        # 3. Freshness filter
        freshness = FreshnessEngine(FreshnessConfig(max_age_hours=48))
        fresh_items = []
        for item in normalized:
            result = freshness.check(item)
            if result.accepted:
                fresh_items.append(item)
        
        # 4. Niche filter
        from newsagent_v2.discovery.niche_filter import RelevanceDecision
        niche = NicheFilter()
        relevant_items = []
        for item in fresh_items:
            decision, reason, confidence = niche.classify(item)
            if decision in (RelevanceDecision.KEEP, RelevanceDecision.CONDITIONAL):
                relevant_items.append(item)
        
        # 5. Deduplicate
        deduper = DeduplicationEngine()
        unique_items, _ = deduper.dedupe_batch(relevant_items)
        
        # 6. Cluster into events
        clusterer = EventClusterer(ClusterConfig())
        events = clusterer.cluster_batch(unique_items)
        
        # 7. Analyze with intelligence
        intelligence = IntelligenceEngine()
        for event in events:
            event.intelligence = intelligence.analyze(event)
        
        # 8. Rank by combined score (breaking + momentum)
        self._ranked_events = self._rank_events(events)
        
        # 9. Save to store
        self.event_store.save_batch(self._ranked_events)
        
        # 10. Save to telegram store for pagination
        self.telegram_store.save_batch(self._ranked_events)
        
        return self._ranked_events
    
    def _rank_events(self, events: list[NewsEvent]) -> list[NewsEvent]:
        """Rank events by intelligence signals."""
        def score(event: NewsEvent) -> float:
            if event.intelligence is None:
                return 0.0
            # Breaking score + momentum + novelty
            s = event.intelligence.breaking.score * 3.0  # Weight breaking heavily
            s += event.intelligence.momentum.score * 2.0
            s += event.intelligence.novelty.score * 1.0
            return s
        
        return sorted(events, key=score, reverse=True)
    
    def get_top_events(self, count: int = 5, offset: int = 0) -> list[NewsEvent]:
        """Get events by rank position."""
        return self._ranked_events[offset:offset + count]
    
    def send_to_telegram(
        self,
        client: TelegramTestClient,
        config: TelegramConfig,
        count: int = 5,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Send ranked events to Telegram as separate cards."""
        events = self.get_top_events(count=count, offset=offset)
        total = len(self._ranked_events)
        
        results = []
        
        for i, event in enumerate(events):
            rank = offset + i + 1
            card = render_card_from_event(event, rank, total)
            
            # Send as message with keyboard
            result = client.send_message(
                chat_id=config.test_chat_id,
                text=card["text"],
                parse_mode=card["parse_mode"],
                reply_markup=card["reply_markup"],
                disable_web_page_preview=True,
            )
            results.append(result)
        
        # After sending last card, add SEE NEXT if there are more
        if offset + count < total and count > 0:
            next_result = client.send_message(
                chat_id=config.test_chat_id,
                text=f"📄 Showing {min(offset + count, total)}/{total} events",
                parse_mode="HTML",
                reply_markup=seepage_keyboard(offset=offset + count),
            )
            results.append(next_result)
        
        return results
    
    def get_cost_report(self) -> dict[str, Any]:
        """Discovery cost report (should be zero)."""
        return {
            "discovery_llm_calls": self._discovery_llm_calls,
            "research_llm_calls": self._research_calls,
            "writer_calls": self._writer_calls,
            "image_network_calls": self._image_calls,
            "estimated_cost_inr": self._cost_inr,
        }
    
    def callback_handler(self) -> Callable[..., dict[str, Any]]:
        """Get callback handler for this pipeline."""
        handler = V5CallbackHandler(
            event_store=self.event_store,
            telegram_store=self.telegram_store,
        )
        return handler.handle


def create_v5_discovery_pipeline(
    event_store: EventStore | None = None,
    source_registry: SourceRegistry | None = None,
    telegram_store: V5TelegramStore | None = None,
) -> V5DiscoveryPipeline:
    """Factory for V5 discovery pipeline."""
    return V5DiscoveryPipeline(
        event_store=event_store,
        source_registry=source_registry,
        telegram_store=telegram_store or V5TelegramStore(),
    )


def execute_v5_make(
    telegram_config: TelegramConfig,
    client: TelegramTestClient,
    event_store: EventStore | None = None,
    source_registry: SourceRegistry | None = None,
) -> dict[str, Any]:
    """Execute V5 /make discovery-only command.
    
    This is the function passed to Telegram listener's execute_make.
    """
    pipeline = create_v5_discovery_pipeline(
        event_store=event_store or EventStore(root=Path("./data/events")),
        source_registry=source_registry,
    )
    
    # Run discovery
    events = pipeline.run_discovery()
    
    # Send Top 5 to Telegram
    results = pipeline.send_to_telegram(
        client=client,
        config=telegram_config,
        count=5,
        offset=0,
    )
    
    return {
        "ok": True,
        "events_count": len(events),
        "cards_sent": len([r for r in results if r.get("ok")]),
        "pipeline": pipeline,  # Return for state preservation
    }
