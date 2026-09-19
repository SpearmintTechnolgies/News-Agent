"""Live V5 Discovery Pipeline - Real RSS Collection"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent / "src"))

print('=' * 70)
print('V5 COLLECTOR V2 - LIVE RSS COLLECTION')
print('=' * 70)
print(f'Start: {datetime.now(timezone.utc).isoformat()}')
print()

# Import V5 discovery components
from newsagent_v2.discovery.collector_v2 import CollectorV2
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.discovery.normalizer import NewsNormalizer
from newsagent_v2.discovery.freshness import FreshnessEngine, FreshnessConfig
from newsagent_v2.discovery.niche_filter import NicheFilter
from newsagent_v2.discovery.deduplicator import DeduplicationEngine
from newsagent_v2.discovery.event_clusterer import EventClusterer
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.intelligence.engine import IntelligenceEngine
from newsagent_v2.v5_telemetry.cost_ledger import CostLedger
from newsagent_v2.v5_telemetry.diagnostics import DiscoveryDiagnostics

start_time = datetime.now(timezone.utc)

# Initialize components
print('Initializing discovery pipeline...')
registry = SourceRegistry()
normalizer = NewsNormalizer()
freshness = FreshnessEngine(FreshnessConfig(max_age_hours=24))
niche = NicheFilter()
dedupe = DeduplicationEngine()
clusterer = EventClusterer()
event_store = EventStore()
intelligence = IntelligenceEngine()
cost_ledger = CostLedger()

diagnostics = DiscoveryDiagnostics()

# Get enabled sources
sources = registry.get_enabled()
print(f'Sources configured: {len(sources)}')
for s in sources:
    print(f'  [{s.source_type}] {s.name}')
    print(f'      URL: {s.url[:60]}...')
print()

# Run collection
print('Starting collection...')
print()

# Import the collect function
from newsagent_v2.discovery.collector_v2 import collect_sources

# This makes actual HTTP requests
try:
    items, diagnostics = collect_sources(
        sources=sources,
        max_workers=4,
        per_feed_limit=30,
    )
    result = {'items': items, 'diagnostics': diagnostics.to_dict() if hasattr(diagnostics, 'to_dict') else vars(diagnostics)}
except Exception as e:
    print(f'COLLECTION ERROR: {type(e).__name__}: {str(e)[:200]}')
    import traceback
    traceback.print_exc()
    sys.exit(1)

print('COLLECTION COMPLETE')
print()

# Get diagnostics - handle both dict and dataclass
if hasattr(result, 'get'):
    diag = result.get('diagnostics', {})
    items = result.get('items', [])
else:
    diag = result
    items = []

# Handle dataclass diagnostics
if hasattr(diag, 'sources_attempted'):
    sources_attempted = diag.sources_attempted
    sources_succeeded = diag.sources_succeeded
    sources_failed = diag.sources_failed
    errors = diag.errors if hasattr(diag, 'errors') else []
    source_results = diag.source_results if hasattr(diag, 'source_results') else []
else:
    sources_attempted = diag.get('sources_attempted', 0)
    sources_succeeded = diag.get('sources_succeeded', 0)
    sources_failed = diag.get('sources_failed', 0)
    errors = diag.get('errors', [])
    source_results = diag.get('source_results', [])

print('1. SOURCES ATTEMPTED:', sources_attempted)
print('2. SOURCES SUCCEEDED:', sources_succeeded)
print('3. SOURCES FAILED:', sources_failed)
print()

# Failure details
print('4. FAILURE DETAILS:')
if errors:
    for e in errors:
        print(f"   Source: {e.get('source_id', 'unknown')}")
        print(f"   Error: {str(e.get('error', 'unknown'))[:100]}")
        print()
elif source_results:
    for r in source_results:
        if hasattr(r, 'success') and not r.success:
            print(f"   Source: {getattr(r, 'source_id', 'unknown')}")
            print(f"   Error: {getattr(r, 'error', 'unknown')[:100]}")
            print()
else:
    print('   No failure details recorded')

print('5. RAW REPORTS COLLECTED:', len(items))

# Show sample raw items
if items:
    print()
    print('Sample raw items:')
    for i, item in enumerate(items[:3], 1):
        print(f"  {i}. {getattr(item, 'headline', str(item))[:80]}...")
    print()

# Process through pipeline
print('PROCESSING PIPELINE...')
print()

# Normalization - already done in collector
normalized_items = items
print('6. NORMALIZED:', len(normalized_items))

# Freshness
fresh_items = []
fresh_rejected = []
fresh_failed = 0
invalid_timestamp = 0
stale_rejected = 0

for item in normalized_items:
    try:
        result = freshness.check(item)
        if result.accepted:
            fresh_items.append(item)
        else:
            fresh_rejected.append((item, result.reason))
            if result.reason and 'too_old' in result.reason:
                stale_rejected += 1
            elif result.reason == 'no_timestamp':
                invalid_timestamp += 1
    except Exception as e:
        fresh_failed += 1
        pass

print('7. FRESH:', len(fresh_items))
if fresh_failed:
    print(f'    (freshness check failed for {fresh_failed} items)')
stale_count = len([r for r in fresh_rejected if r[1] and 'too_old' in r[1]])
invalid_ts = len([r for r in fresh_rejected if r[1] == 'no_timestamp'])
print(f'    Stale (>24h): {stale_count}')
print(f'    Invalid timestamp: {invalid_ts}')

# Niche filtering
from newsagent_v2.discovery.niche_filter import RelevanceDecision

relevant_items = []
niche_rejected = []
for item in fresh_items:
    try:
        decision_result, reason, confidence = niche.classify(item)
        # Keep both KEEP and CONDITIONAL
        if decision_result in (RelevanceDecision.KEEP, RelevanceDecision.CONDITIONAL):
            relevant_items.append(item)
        else:
            niche_rejected.append((item, reason, decision_result))
    except Exception as e:
        import traceback
        traceback.print_exc()
        pass

print('8. NICHE RELEVANT:', len(relevant_items))
print(f'    (rejected: {len(niche_rejected)})')

# Deduplication
try:
    unique_items_tuple = dedupe.dedupe_batch(relevant_items)
    # Returns tuple of (unique_items, duplicates)
    if isinstance(unique_items_tuple, tuple):
        unique_items = unique_items_tuple[0]
    else:
        unique_items = unique_items_tuple
    duplicates_removed = len(relevant_items) - len(unique_items)
except Exception as e:
    print(f'DEDUPE ERROR: {e}')
    import traceback
    traceback.print_exc()
    unique_items = relevant_items
    duplicates_removed = 0

print('9. EXACT DUPLICATES REMOVED:', max(0, duplicates_removed))
print('10. REPORTS AFTER DEDUPE:', len(unique_items))

# Event clustering
print()
print('CLUSTERING INTO EVENTS...')
try:
    events = clusterer.cluster_batch(unique_items)
except Exception as e:
    print(f'CLUSTERING ERROR: {e}')
    import traceback
    traceback.print_exc()
    events = []

# Store events and check for existing
new_events = 0
updated_events = 0
meaningful_developments = 0

try:
    for event in events:
        try:
            existing = event_store.get(event.event_id)
            if existing:
                # Check if new development
                if hasattr(existing, 'developments') and hasattr(event, 'developments'):
                    if len(event.developments) > len(existing.developments):
                        meaningful_developments += len(event.developments) - len(existing.developments)
                event_store.save(event)
                updated_events += 1
            else:
                event_store.save(event)
                new_events += 1
        except Exception as e:
            print(f'Store error for event {event.event_id}: {e}')
            new_events += 1  # Assume new
except Exception as e:
    print(f'Event store error: {e}')

print('11. NEW EVENTS:', new_events)
print('12. EXISTING EVENTS UPDATED:', updated_events)
print('13. MEANINGFUL DEVELOPMENTS:', meaningful_developments)
print('14. TOTAL NEWSEVENTS:', len(events))

# Runtime
end_time = datetime.now(timezone.utc)
duration = (end_time - start_time).total_seconds()
print()
print('15. DISCOVERY RUNTIME:', f'{duration:.1f}s')
print()

# Show 10 events with details
if events:
    print('=' * 70)
    print('REAL NEWSEVENTS (up to 10):')
    print('=' * 70)
    print()

    for i, event in enumerate(events[:10], 1):
        try:
            # Get intelligence
            intel = intelligence.analyze(event) if intelligence else None

            print(f"{i}. Event ID: {event.event_id}")
            print(f"   Title: {event.canonical_title}")
            print(f"   Report count: {event.source_count}")

            # Source names
            source_names = list(set(r.source for r in event.reports)) if event.reports else []
            print(f"   Independent sources: {len(source_names)}")
            print(f"   Source names: {', '.join(source_names[:5])}")

            print(f"   Topic: {event.topic}")
            print(f"   Entities: {', '.join(list(event.entities)[:8]) if event.entities else 'N/A'}")

            if intel:
                print(f"   Momentum: {getattr(intel, 'momentum', 'N/A')}")
                print(f"   Novelty: {getattr(intel, 'novelty', 'N/A')}")
                breaking = getattr(intel, 'breaking_signal', None)
                if breaking:
                    print(f"   Breaking: {getattr(breaking, 'is_breaking', False)}")

            print(f"   First seen: {event.first_seen}")

            # Underlying report headlines
            if event.reports:
                print("   Report headlines:")
                for r in event.reports[:5]:
                    headline = getattr(r, 'headline', str(r))[:70]
                    print(f"      - {headline}...")

            print()
        except Exception as e:
            print(f"Error displaying event {i}: {e}")
            print()

    # Quality analysis
    print('=' * 70)
    print('QUALITY ANALYSIS:')
    print('=' * 70)
    print()

    # Look for potential issues
    potential_duplicates = []
    suspicious_clusters = []
    false_positives = []

    for i, event in enumerate(events):
        for j, other_event in enumerate(events[i+1:], i+1):
            # Check for potential duplicates
            if event.canonical_title and other_event.canonical_title:
                similarity = len(set(event.canonical_title.lower().split()) &
                               set(other_event.canonical_title.lower().split())) / \
                           max(len(event.canonical_title.split()),
                               len(other_event.canonical_title.split()))
                if similarity > 0.5 and event.event_id != other_event.event_id:
                    potential_duplicates.append((event.event_id, other_event.event_id,
                                               event.canonical_title[:50]))

    if potential_duplicates:
        print('LIKELY UNDER-CLUSTERING (duplicate events):')
        for dup in potential_duplicates[:5]:
            print(f"  - Events {dup[0]} and {dup[1]}: {dup[2]}...")
        print()
    else:
        print('LIKELY UNDER-CLUSTERING: None detected')
        print()

    print('LIKELY OVER-CLUSTERING: Manual review needed')
    print('FALSE POSITIVE NICHE: Manual review needed')
    print()

# Cost verification
cost_report = cost_ledger.get_report()
print()
print('=' * 70)
print('PROVIDER LEDGER')
print('=' * 70)
print(f'discovery_llm_calls: {cost_report.discovery_llm_calls}')
print(f'research_llm_calls: {cost_report.research_llm_calls}')
print(f'writer_calls: {cost_report.writer_calls}')
print(f'image_network_calls: {cost_report.image_network_calls}')
est_cost = getattr(cost_report, 'estimated_cost_inr', getattr(cost_report, 'total_cost_inr', 0))
print(f'estimated discovery cost: INR {est_cost}')
print()

if cost_report.discovery_llm_calls == 0 and cost_report.writer_calls == 0 and cost_report.image_network_calls == 0:
    print('[OK] COST BOUNDARY: Zero paid calls verified')
else:
    print('[FAIL] COST BOUNDARY VIOLATED')

print()
print('=' * 70)
print('DISCOVERY COMPLETE')
print('=' * 70)
