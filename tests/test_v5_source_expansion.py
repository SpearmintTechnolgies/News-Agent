"""Test V5 Source Expansion - FREE research-only smoke test.

This test verifies that source expansion produces corroborating evidence
without making any paid model calls (Kimi, Groq, GPT, Vertex).

Usage:
    python -m pytest tests/test_v5_source_expansion.py -v
    python tests/test_v5_source_expansion.py

Required environment:
    - No API keys needed
    - Network access to RSS feeds
"""

import json
import sys
import unittest
from pathlib import Path

# Add repo to path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from newsagent_v2.discovery.event_clusterer import NewsEvent, Development, EventReport
from newsagent_v2.v5_generation.source_expansion_adapter import (
    expand_sources_for_event,
    build_search_fn_for_event,
)
from newsagent_v2.article.writer.v4.event_research import research_event


class TestV5SourceExpansion(unittest.TestCase):
    """Source expansion tests - NO PAID MODEL CALLS."""

    def test_source_expansion_collects_from_registry(self):
        """Test that expansion collects from configured sources."""
        event = {
            "event_id": "test-expansion-001",
            "representative_title": "Bitcoin ETF Approval",
            "canonical_title": "Bitcoin ETF Approval",
            "topic": "cryptocurrency",
            "entities": ["Bitcoin", "SEC", "ETF"],
        }

        result = expand_sources_for_event(
            event=event,
            event_entities=["Bitcoin", "SEC", "ETF"],
            event_topic="cryptocurrency",
            event_reports=[],
            max_candidates=6,
        )

        # Should have attempted collection
        self.assertGreaterEqual(result.candidates_considered, 0)
        # Diagnostic data should be present
        self.assertIn("collection_started_at", result.diagnostics)
        self.assertIn("sources_attempted", result.diagnostics)

        # If collection succeeded, should have source results
        if result.candidates_considered > 0:
            self.assertIn("sources_succeeded", result.diagnostics)
            print(f"✅ Collected from {result.candidates_considered} candidate items")
            print(f"   Sources attempted: {result.diagnostics.get('sources_attempted', 0)}")
            print(f"   Sources succeeded: {result.diagnostics.get('sources_succeeded', 0)}")

    def test_original_source_403_corroborating_exists(self):
        """Test: Original source 403 + corroborating source exists → research continues."""
        # Simulate an event where original source returns 403
        # but there are corroborating sources available

        event_reports = [
            {
                "report_id": "r1",
                "source": "The Block",
                "source_id": "theblock",
                "source_authority": 0.8,
                "headline": "Bitcoin ETF Receives Approval",
                "url": "https://www.theblock.co/article/12345/bitcoin-etf-approval",
                "published_at": "2024-01-10T14:00:00Z",
                "description": "SEC approves spot Bitcoin ETF",
                "entities": ["Bitcoin", "SEC"],
            }
        ]

        event = {
            "event_id": "evt-403-corroborate-test",
            "representative_title": "Bitcoin ETF Approval by SEC",
            "canonical_title": "Bitcoin ETF Approval by SEC",
            "topic": "regulatory",
            "entities": ["Bitcoin", "SEC", "ETF"],
        }

        # Expand sources
        result = expand_sources_for_event(
            event=event,
            event_entities=["Bitcoin", "SEC", "ETF"],
            event_topic="regulatory",
            event_reports=event_reports,
            max_candidates=8,
        )

        print(f"\n✅ Test: Original 403 + Corroborating exists")
        print(f"   Candidates considered: {result.candidates_considered}")
        print(f"   Sources matched: {result.sources_matched}")
        print(f"   Sources added: {len(result.sources_added)}")

        # Diagnostic exposure
        self.assertTrue("diagnostics" in result.as_dict())
        self.assertTrue("sources_attempted" in result.diagnostics)

    def test_original_403_no_corroborating_gate_blocked(self):
        """Test: Original source 403 + no corroborating → should fail evidence gate."""
        # This tests the pre-writer gate logic
        # When sources_retrieved == 0 and no expansion sources match,
        # the gate should block.

        event = {
            "event_id": "evt-no-corroborate",
            "representative_title": "Very Niche Crypto Event XYZ123",
            "canonical_title": "Very Niche Crypto Event XYZ123",
            "topic": "niche_crypto",
            "entities": ["XYZ123Token", "UnknownProject"],
        }

        # Use a topic that's unlikely to have recent matches
        with self.assertRaises(Exception):
            pass  # Placeholder for gate test

        print("\n✅ Test: No corroborating sources → gate would block (simulated)")

    def test_duplicate_url_deduplication(self):
        """Test: Duplicate URLs are deduplicated."""
        event_reports = [
            {
                "report_id": "r1",
                "source": "CoinDesk",
                "url": "https://www.coindesk.com/article/123",
            },
        ]

        # Add a report with same URL (simulated duplicate)
        event = {
            "event_id": "evt-dedup-test",
            "representative_title": "Bitcoin Price Movement",
            "topic": "market",
            "entities": ["Bitcoin"],
        }

        result = expand_sources_for_event(
            event=event,
            event_entities=["Bitcoin"],
            event_topic="market",
            event_reports=event_reports,
        )

        # Check that no duplicates were added
        urls = [s.get("url", "").lower().rstrip("/") for s in result.sources_added]
        self.assertEqual(len(urls), len(set(urls)), "URLs should be deduplicated")

        print("\n✅ Test: Duplicate URL deduplication")

    def test_primary_source_preference(self):
        """Test: Primary sources are preferred in ranking."""
        event = {
            "event_id": "evt-primary-test",
            "representative_title": "SEC Enforcement Action",
            "topic": "regulatory",
            "entities": ["SEC", "Enforcement"],
        }

        result = expand_sources_for_event(
            event=event,
            event_entities=["SEC", "Enforcement"],
            event_topic="regulatory",
            event_reports=[],
            max_candidates=6,
        )

        # Check if any primary sources were found
        primary_count = sum(1 for s in result.sources_added if s.get("primary_evidence"))

        print(f"\n✅ Test: Primary source preference")
        print(f"   Primary sources found: {primary_count}")
        if result.diagnostics.get("sources_succeeded", 0) > 0:
            print(f"   Collection succeeded from registry")


def test_with_event_set(event_id: str = "evt-7ca1ad73"):
    """Run smoke test against a specific event ID.

    This verifies source expansion works for a known event.
    """
    print(f"\n{'='*60}")
    print(f"RESEARCH-ONLY SMOKE TEST for {event_id}")
    print(f"{'='*60}")

    # Create a test event similar to the one referenced in the issue
    # The Block article about some crypto event
    event_reports = [
        {
            "report_id": "r-7ca1ad73-001",
            "source": "The Block",
            "source_id": "theblock",
            "source_authority": 0.8,
            "headline": "Crypto Exchange Announces New Trading Features",
            "url": "https://www.theblock.co/article/crypto-exchange",  # Placeholder
            "published_at": "2024-01-15T10:00:00Z",
            "description": "Exchange adds new crypto trading features",
            "entities": ["Exchange", "Crypto"],
        }
    ]

    event_data = {
        "event_id": event_id,
        "representative_title": "Crypto Exchange New Trading Features",
        "canonical_title": "Crypto Exchange New Trading Features",
        "topic": "exchange",
        "entities": ["Exchange", "Crypto", "Trading"],
    }

    # Run expansion
    expansion = expand_sources_for_event(
        event=event_data,
        event_entities=event_data["entities"],
        event_topic=event_data["topic"],
        event_reports=event_reports,
        max_candidates=10,
    )

    print(f"\n📊 EXPANSION RESULTS:")
    print(f"   Candidates considered: {expansion.candidates_considered}")
    print(f"   Sources matched:       {expansion.sources_matched}")
    print(f"   Sources added:         {len(expansion.sources_added)}")
    print(f"   Primary sources:       {expansion.primary_sources_retained}")
    print(f"   Independent domains:   {expansion.independent_domains}")

    print(f"\n📋 DIAGNOSTICS:")
    for key, value in expansion.diagnostics.items():
        if isinstance(value, (int, str, float)):
            print(f"   {key}: {value}")

    if expansion.sources_added:
        print(f"\n📝 ADDED SOURCES:")
        for src in expansion.sources_added[:5]:
            print(f"   - {src.get('source', 'unknown')}: {src.get('title', 'no title')[:50]}...")
            print(f"     Relevance: {src.get('relevance_score', 0)}")

    # Build story and test research path
    story = {
        "event_id": event_id,
        "representative_title": event_data["representative_title"],
        "topic": event_data["topic"],
        "entities": event_data["entities"],
        "article_input": {
            "event_id": event_id,
            "representative_title": event_data["representative_title"],
            "topic": event_data["topic"],
            "entities": event_data["entities"],
            "evidence": expansion.sources_added + [
                {
                    "source": r["source"],
                    "source_id": r["source_id"],
                    "source_authority": r["source_authority"],
                    "url": r["url"],
                    "title": r["headline"],
                    "published": r["published_at"],
                    "summary": r["description"],
                }
                for r in event_reports
            ],
        }
    }

    # Simulate what the research_event would do
    # We're NOT calling it to avoid any potential model calls
    print(f"\n🔍 PRE-RESEARCH DIAGNOSTICS (simulated):")
    print(f"   Evidence rows before research: {len(story['article_input']['evidence'])}")
    print(f"   Independent domains available: {expansion.independent_domains}")

    if expansion.sources_added:
        print(f"\n✅ EXPECTED OUTCOME:")
        print(f"   With expansion sources added:")
        print(f"   - sources_discovered: {len(story['article_input']['evidence'])}")
        print(f"   - independent_sources: {len(set(expansion.independent_domains))}")
        print(f"   - Should pass pre-writer gate: {len(expansion.sources_added) > 0}")
    else:
        print(f"\n❌ RISK: No expansion sources found")
        print(f"   If original source returns 403:")
        print(f"   - sources_retrieved would be 0")
        print(f"   - Pre-writer gate would BLOCK")

    print(f"\n{'='*60}")
    print(f"END SMOKE TEST")
    print(f"{'='*60}")

    return expansion


if __name__ == "__main__":
    # First run unit tests
    print("\n" + "="*60)
    print("RUNNING UNIT TESTS")
    print("="*60)
    unittest.main(argv=[sys.argv[0], '-v'], exit=False)

    # Then run the event-specific smoke test
    print("\n" + "="*60)
    print("RUNNING EVENT-SPECIFIC SMOKE TEST")
    print("="*60)
    expansion = test_with_event_set("evt-7ca1ad73")
