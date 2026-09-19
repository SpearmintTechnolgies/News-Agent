"""Debug niche filter"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from newsagent_v2.discovery.collector_v2 import collect_sources
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.discovery.freshness import FreshnessEngine, FreshnessConfig
from newsagent_v2.discovery.niche_filter import NicheFilter

# Get real items
registry = SourceRegistry()
sources = registry.get_enabled()
items, diag = collect_sources(sources)

# Apply freshness
freshness = FreshnessEngine(FreshnessConfig(max_age_hours=24))
fresh_items = [item for item in items if freshness.check(item).accepted]
print(f"Fresh items: {len(fresh_items)}")

# Test niche filter on first few
niche = NicheFilter()
print("\nChecking first 5 fresh items:")
for i, item in enumerate(fresh_items[:5], 1):
    decision = niche.assess(item)
    print(f"\n{i}. {item.headline[:60]}...")
    print(f"   Entities: {item.entities}")
    print(f"   Decision: is_relevant={decision.is_relevant}")
    print(f"   Reason: {decision.reason}")
    print(f"   Confidence: {decision.confidence}")
