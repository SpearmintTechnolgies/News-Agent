"""Live Collector V2 test script - Phase 2"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.discovery.normalizer import NewsNormalizer
from newsagent_v2.discovery.freshness import FreshnessEngine
from newsagent_v2.discovery.niche_filter import NicheFilter
from newsagent_v2.discovery.deduplicator import DeduplicationEngine
from newsagent_v2.discovery.event_clusterer import EventClusterer
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.intelligence.engine import IntelligenceEngine
from newsagent_v2.v5_telemetry.cost_ledger import CostLedger

async def run_live_collector():
    """Run live RSS collection test."""
    print("=" * 60)
    print("PHASE 2: LIVE COLLECTOR V2 TEST")
    print("=" * 60)
    print()

    start_time = datetime.now(timezone.utc)

    # Initialize components
    registry = SourceRegistry()
    normalizer = NewsNormalizer()
    freshness = FreshnessEngine()
    niche = NicheFilter()
    dedupe = DeduplicationEngine()
    clusterer = EventClusterer()
    event_store = EventStore()
    intelligence = IntelligenceEngine()
    cost_ledger = CostLedger()

    # Get enabled sources
    sources = registry.get_enabled()
    print(f"Sources configured: {len(sources)}")

    source_types = {}
    for s in sources:
        # Handle both dict and dataclass
        if hasattr(s, 'source_type'):
            st = s.source_type
        elif isinstance(s, dict):
            st = s.get("source_type", "unknown")
        else:
            st = "unknown"
        source_types[st] = source_types.get(st, 0) + 1
    for st, count in source_types.items():
        print(f"  - {st}: {count}")

    print()

    # Initialize collector
    collector = CollectorV2(
        source_registry=registry,
        normalizer=normalizer,
    )

    # Collect with diagnostics
    print("Collecting from sources...")
    result = await collector.collect_all()

    diagnostics = result.get("diagnostics", {})
    raw_items = result.get("items", [])

    print(f"Sources attempted: {diagnostics.get('sources_attempted', 0)}")
    print(f"Sources succeeded: {diagnostics.get('sources_succeeded', 0)}")
    print(f"Sources failed: {diagnostics.get('sources_failed', 0)}")
    print(f"Raw reports collected: {len(raw_items)}")
    print()

    # Print failures
    failures = diagnostics.get("failures", [])
    if failures:
        print("Source failures:")
        for f in failures[:5]:  # Limit to first 5
            print(f"  - {f.get('source', 'unknown')}: {f.get('category', 'unknown')} - {f.get('reason', 'unknown')[:100]}")
        print()

    # Process through pipeline
    print("Processing through pipeline...")

    # Freshness
    fresh_items = []
    for item in raw_items:
        try:
            if freshness.is_fresh(item):
                fresh_items.append(item)
        except Exception as e:
            pass

    print(f"Fresh reports: {len(fresh_items)}")

    # Niche filter
    relevant_items = []
    for item in fresh_items:
        decision = niche.assess(item)
        if decision.is_relevant:
            relevant_items.append(item)

    print(f"Niche-relevant reports: {len(relevant_items)}")

    # Deduplication
    dedupe_result = dedupe.deduplicate_batch(relevant_items)
    unique_items = dedupe_result.unique_items
    duplicates_removed = len(relevant_items) - len(unique_items)

    print(f"Exact duplicates removed: {duplicates_removed}")
    print(f"Reports after dedupe: {len(unique_items)}")
    print()

    # Event clustering
    print("Clustering into events...")
    events = clusterer.cluster_to_events(unique_items)

    new_events = 0
    updated_events = 0
    for event in events:
        existing = event_store.load(event.event_id)
        if existing:
            event_store.save(event)  # Merge/update
            updated_events += 1
        else:
            event_store.save(event)
            new_events += 1

    print(f"New events: {new_events}")
    print(f"Existing events updated: {updated_events}")

    # Meaningful developments
    total_developments = sum(len(e.developments) for e in events)
    print(f"Meaningful developments: {total_developments}")
    print()

    # Intelligence analysis
    print("Running intelligence analysis...")
    for event in events[:10]:  # Limit to first 10 for display
        intel = intelligence.analyze(event)
        event.intelligence = intel
        event_store.save(event)

    print(f"Events with intelligence: {min(len(events), 10)}")
    print()

    # Cost verification
    cost_report = cost_ledger.get_report()
    print("COST BOUNDARY VERIFICATION:")
    print(f"  Discovery LLM calls: {cost_report.get('discovery_llm_calls', 0)}")
    print(f"  Writer calls: {cost_report.get('writer_calls', 0)}")
    print(f"  Image calls: {cost_report.get('image_calls', 0)}")
    print(f"  Estimated discovery cost: ₹{cost_report.get('estimated_cost_inr', 0)}")
    print()

    # Duration
    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()
    print(f"Duration: {duration:.1f}s")
    print()

    # Sample events for display
    print("=" * 60)
    print("SAMPLE EVENTS (up to 10):")
    print("=" * 60)
    for i, event in enumerate(events[:10], 1):
        intel = getattr(event, "intelligence", None)
        print(f"\n{i}. Event ID: {event.event_id}")
        print(f"   Title: {event.canonical_title[:80]}...")
        print(f"   Reports: {event.source_count}")
        sources = list(set(r.source for r in event.reports))[:5]
        print(f"   Sources: {', '.join(sources)}")
        print(f"   Topic: {event.topic}")
        print(f"   Entities: {', '.join(list(event.entities)[:5])}")
        if intel:
            print(f"   Momentum: {getattr(intel, 'momentum', 'N/A')}")
            print(f"   Novelty: {getattr(intel, 'novelty', 'N/A')}")
            breaking = getattr(intel, 'breaking_signal', None)
            if breaking and getattr(breaking, 'is_breaking', False):
                print(f"   Breaking: YES")
        print(f"   First seen: {event.first_seen}")

    print()
    print("=" * 60)
    print("COLLECTOR TEST COMPLETE")
    print("=" * 60)

    return {
        "sources_attempted": diagnostics.get("sources_attempted", 0),
        "sources_succeeded": diagnostics.get("sources_succeeded", 0),
        "sources_failed": diagnostics.get("sources_failed", 0),
        "raw_reports": len(raw_items),
        "fresh_reports": len(fresh_items),
        "relevant_reports": len(relevant_items),
        "after_dedupe": len(unique_items),
        "new_events": new_events,
        "updated_events": updated_events,
        "developments": total_developments,
        "events_presented": len(events),
        "duration": duration,
        "cost_report": cost_report,
        "events": events,
    }

if __name__ == "__main__":
    result = asyncio.run(run_live_collector())
