"""Debug freshness handling"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from newsagent_v2.discovery.collector_v2 import collect_sources
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.discovery.freshness import FreshnessEngine, FreshnessConfig

# Get real items
registry = SourceRegistry()
sources = registry.get_enabled()
print(f"Sources: {len(sources)}")

items, diag = collect_sources(sources)
print(f"Items collected: {len(items)}")

if items:
    print("\nChecking first 3 items:")
    for i, item in enumerate(items[:3], 1):
        print(f"\n{i}. Headline: {item.headline[:60]}...")
        print(f"   published_at: {repr(item.published_at)}")
        print(f"   Type: {type(item.published_at)}")

        # Try age_hours
        try:
            age = item.age_hours()
            print(f"   age_hours: {age}")
        except Exception as e:
            print(f"   age_hours ERROR: {type(e).__name__}: {e}")

        # Now test freshness
        freshness = FreshnessEngine(FreshnessConfig(max_age_hours=24))
        try:
            result = freshness.check(item)
            print(f"   FreshnessResult: accepted={result.accepted}, reason={result.reason}")
        except Exception as e:
            print(f"   freshness ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
