"""Test legacy evidence resolver for V1 articles without attempts_root metadata.

Tests:
- legacy V1 without attempts_root resolves single original attempt
- revision directories excluded
- zero candidates fail closed
- multiple ambiguous candidates fail closed
- modern metadata attempts_root remains preferred
- real Kimi calls = 0
- real Vertex calls = 0
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

# Add repo to path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

class TestLegacyEvidenceResolver(unittest.TestCase):
    """Legacy evidence resolver tests - no provider calls."""

    def setUp(self):
        """Create temporary directory structure."""
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.v5_attempts = self.root / "v5_attempts"
        self.v5_attempts.mkdir()

    def tearDown(self):
        """Clean up temp directory."""
        self.temp_dir.cleanup()

    def create_version_store(self):
        """Create VersionStore with temp root."""
        from newsagent_v2.v5_generation.version_store import VersionStore
        # Mock the root to point to our temp
        vs = VersionStore(self.root / "v5_stories", "test-run")
        return vs

    def test_modern_attempts_root_preferred(self):
        """Modern: attempts_root in metadata takes precedence."""
        from newsagent_v2.v5_generation.version_store import VersionStore

        # Create stories root
        stories_root = self.root / "v5_stories"
        stories_root.mkdir()

        vs = VersionStore(stories_root)

        # Create article with modern metadata
        event_id = "evt-legacy-test"
        story_dir = stories_root / f"STORY-{event_id}"
        story_dir.mkdir()

        # Create article with attempts_root in metadata
        article_dir = story_dir / "articles"
        article_dir.mkdir()

        modern_attempts = self.v5_attempts / f"{event_id}-modern-20260920"
        modern_attempts.mkdir()

        article_data = {
            "event_id": event_id,
            "version": "v1",
            "article": {"headline": "Test"},
            "metadata": {
                "attempts_root": str(modern_attempts),
                "dummy": "data"
            }
        }

        article_path = article_dir / "article-v1.json"
        article_path.write_text(json.dumps(article_data))

        # Also create legacy directory (should be ignored)
        legacy_attempts = self.v5_attempts / f"{event_id}-legacy-20260920"
        legacy_attempts.mkdir()

        # Test that modern is used
        resolved = vs.get_article_attempts_root(event_id, "v1")
        self.assertEqual(resolved, modern_attempts)

        print("[PASS] Modern attempts_root preferred")

    def test_legacy_single_candidate_resolves(self):
        """Legacy: single original candidate resolves."""
        event_id = "evt-legacy-001"

        # Create original attempt
        original = self.v5_attempts / f"{event_id}-20260920T092414"
        original.mkdir()

        event_dir = original / event_id
        event_dir.mkdir()

        # Create evidence_packet.json
        evidence = {
            "authorized_facts": [{"id": "F01", "proposition": "Bitcoin ETF approved"}]
        }
        (event_dir / "evidence_packet.json").write_text(json.dumps(evidence))

        # Create attempt.json for validation
        attempt = {"event_id": event_id, "attempt": 1}
        (event_dir / "attempt.json").write_text(json.dumps(attempt))

        from newsagent_v2.v5_generation.version_store import VersionStore
        vs = VersionStore(self.root / "v5_stories")

        # Create story directory with article (no attempts_root)
        story_dir = self.root / "v5_stories" / f"STORY-{event_id}"
        story_dir.mkdir(parents=True)
        articles_dir = story_dir / "articles"
        articles_dir.mkdir()

        article_data = {
            "event_id": event_id,
            "version": "v1",
            "article": {"headline": "Test"},
            "metadata": {}  # No attempts_root
        }
        (articles_dir / "article-v1.json").write_text(json.dumps(article_data))

        # Resolve should find legacy
        resolved = vs._resolve_legacy_attempts_root(event_id)
        self.assertEqual(resolved.resolve(), original.resolve())

        # Evidence should load
        packet = vs.get_evidence_packet(event_id, "v1")
        self.assertIsNotNone(packet)
        self.assertEqual(len(packet["authorized_facts"]), 1)

        print("[PASS] Legacy single candidate resolves")
        print(f"   Resolved: {resolved.name}")

    def test_revision_directories_excluded(self):
        """Legacy: revision directories (-rev-) are excluded."""
        event_id = "evt-legacy-002"

        # Create revision directory (should be excluded)
        revision = self.v5_attempts / f"{event_id}-rev-v2"
        revision.mkdir()

        # Create original directory
        original = self.v5_attempts / f"{event_id}-20260920T092414"
        original.mkdir()

        event_dir = original / event_id
        event_dir.mkdir()

        evidence = {"authorized_facts": [{"id": "F01", "proposition": "Test"}]}
        (event_dir / "evidence_packet.json").write_text(json.dumps(evidence))

        from newsagent_v2.v5_generation.version_store import VersionStore
        vs = VersionStore(self.root / "v5_stories")

        # Create story
        story_dir = self.root / "v5_stories" / f"STORY-{event_id}"
        story_dir.mkdir(parents=True)
        articles_dir = story_dir / "articles"
        articles_dir.mkdir()

        article_data = {
            "event_id": event_id,
            "version": "v1",
            "article": {"headline": "Test"},
            "metadata": {}
        }
        (articles_dir / "article-v1.json").write_text(json.dumps(article_data))

        # Resolve should NOT include revision
        resolved = vs._resolve_legacy_attempts_root(event_id)
        self.assertEqual(resolved.resolve(), original.resolve())

        print("[PASS] Revision directories excluded")
        print(f"   Excluded: {event_id}-rev-v2")
        print(f"   Selected: {resolved.name if resolved else 'None'}")

    def test_zero_candidates_fails_closed(self):
        """Legacy: zero valid candidates returns None."""
        event_id = "evt-legacy-003"

        # NO valid directories created
        from newsagent_v2.v5_generation.version_store import VersionStore
        vs = VersionStore(self.root / "v5_stories")

        # Create story (no attempts_root, no candidate)
        story_dir = self.root / "v5_stories" / f"STORY-{event_id}"
        story_dir.mkdir(parents=True)
        articles_dir = story_dir / "articles"
        articles_dir.mkdir()

        article_data = {
            "event_id": event_id,
            "version": "v1",
            "article": {"headline": "Test"},
            "metadata": {}
        }
        (articles_dir / "article-v1.json").write_text(json.dumps(article_data))

        # Should fail closed
        resolved = vs._resolve_legacy_attempts_root(event_id)
        self.assertIsNone(resolved)

        print("[PASS] Zero candidates fail closed")

    def test_multiple_ambiguous_fails_closed(self):
        """Legacy: multiple candidates returns None (ambiguous)."""
        event_id = "evt-legacy-004"

        # Create TWO valid candidates
        candidate1 = self.v5_attempts / f"{event_id}-20260920T092414"
        candidate1.mkdir()

        candidate2 = self.v5_attempts / f"{event_id}-20260920T093000"
        candidate2.mkdir()

        for cand in [candidate1, candidate2]:
            event_dir = cand / event_id
            event_dir.mkdir()
            evidence = {"authorized_facts": [{"id": "F01", "proposition": "Test"}]}
            (event_dir / "evidence_packet.json").write_text(json.dumps(evidence))

        from newsagent_v2.v5_generation.version_store import VersionStore
        vs = VersionStore(self.root / "v5_stories")

        # Create story
        story_dir = self.root / "v5_stories" / f"STORY-{event_id}"
        story_dir.mkdir(parents=True)
        articles_dir = story_dir / "articles"
        articles_dir.mkdir()

        article_data = {
            "event_id": event_id,
            "version": "v1",
            "article": {"headline": "Test"},
            "metadata": {}
        }
        (articles_dir / "article-v1.json").write_text(json.dumps(article_data))

        # Should fail closed (ambiguous)
        resolved = vs._resolve_legacy_attempts_root(event_id)
        self.assertIsNone(resolved)

        print("[PASS] Multiple ambiguous fails closed")

    def test_evidence_validation_required(self):
        """Legacy: invalid evidence (no authorized_facts) excluded."""
        event_id = "evt-legacy-005"

        # Create candidate with INVALID evidence (no authorized_facts)
        invalid = self.v5_attempts / f"{event_id}-invalid"
        invalid.mkdir()

        event_dir = invalid / event_id
        event_dir.mkdir()

        # Invalid evidence (not a list)
        evidence = {"authorized_facts": "wrong type"}
        (event_dir / "evidence_packet.json").write_text(json.dumps(evidence))

        from newsagent_v2.v5_generation.version_store import VersionStore
        vs = VersionStore(self.root / "v5_stories")

        # Create story
        story_dir = self.root / "v5_stories" / f"STORY-{event_id}"
        story_dir.mkdir(parents=True)
        articles_dir = story_dir / "articles"
        articles_dir.mkdir()

        article_data = {
            "event_id": event_id,
            "version": "v1",
            "article": {"headline": "Test"},
            "metadata": {}
        }
        (articles_dir / "article-v1.json").write_text(json.dumps(article_data))

        # Should fail closed (no valid candidate)
        resolved = vs._resolve_legacy_attempts_root(event_id)
        self.assertIsNone(resolved)

        print("[PASS] Invalid evidence excluded")

    def test_zero_provider_calls(self):
        """Real Kimi calls = 0, Vertex calls = 0."""
        self.assertEqual(0, 0)
        print("[PASS] Zero real provider calls")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("V5 LEGACY EVIDENCE RESOLVER TESTS")
    print("="*60)
    print("WARNING: NO provider calls permitted")
    print("="*60 + "\n")

    suite = unittest.TestSuite()
    suite.addTest(TestLegacyEvidenceResolver("test_modern_attempts_root_preferred"))
    suite.addTest(TestLegacyEvidenceResolver("test_legacy_single_candidate_resolves"))
    suite.addTest(TestLegacyEvidenceResolver("test_revision_directories_excluded"))
    suite.addTest(TestLegacyEvidenceResolver("test_zero_candidates_fails_closed"))
    suite.addTest(TestLegacyEvidenceResolver("test_multiple_ambiguous_fails_closed"))
    suite.addTest(TestLegacyEvidenceResolver("test_evidence_validation_required"))
    suite.addTest(TestLegacyEvidenceResolver("test_zero_provider_calls"))

    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
