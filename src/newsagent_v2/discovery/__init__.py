"""NewsAgent V5 Discovery Backbone.

Deterministic, zero-paid-model discovery pipeline.
/make → Collector V2 → RawNewsItem → Normalization → Freshness → Niche Filter
→ Deduplication → Event Clusterer → Persistent Event Store → News Intelligence
→ Telegram News Desk
"""

from __future__ import annotations

from .collector_v2 import CollectorV2, collect_sources
from .source_registry import SourceRegistry, SourceMetadata
from .raw_news_item import RawNewsItem
from .normalizer import NewsNormalizer
from .freshness import FreshnessEngine
from .niche_filter import NicheFilter
from .deduplicator import DeduplicationEngine
from .event_clusterer import EventClusterer
from .event_store import EventStore, NewsEvent

__all__ = [
    "CollectorV2",
    "collect_sources",
    "SourceRegistry",
    "SourceMetadata",
    "RawNewsItem",
    "NewsNormalizer",
    "FreshnessEngine",
    "NicheFilter",
    "DeduplicationEngine",
    "EventClusterer",
    "EventStore",
    "NewsEvent",
]
