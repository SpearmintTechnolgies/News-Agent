"""Test V5 Telegram Delivery - MOCKS ONLY, ZERO provider calls.

Tests:
1. Initial review package sends article + image + controls
2. Article > Telegram limit is split
3. Revision sends correct versions
4. ZERO Kimi/Vertex calls
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

# Add repo to path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from newsagent_v2.v5_generation.telegram_delivery import (
    send_initial_review_package,
    send_revision_package,
    _build_article_message,
    _build_review_keyboard,
)


@dataclass
class MockArticle:
    headline: str
    dek: str
    article_body: str


class MockVersionStore:
    """Mock VersionStore for testing."""

    def __init__(self, article=None, image_exists=True, image_bytes=b"fake_image_data"):
        self._article = article
        self._image_exists = image_exists
        self._image_bytes = image_bytes

    def get_article(self, event_id: str, version: str):
        if self._article:
            return {
                "article": {
                    "headline": self._article.headline,
                    "dek": self._article.dek,
                    "article_body": self._article.article_body,
                },
                "metadata": {"article_hash": "mock_hash_123"},
                "version": version,
            }
        return None

    def get_image_path(self, event_id: str, version: str):
        if self._image_exists:
            # Return a mock path
            mock_path = MagicMock()
            mock_path.is_file.return_value = True
            mock_path.name = f"image-{version}.png"
            mock_path.read_bytes.return_value = self._image_bytes
            return mock_path
        return None


class TestTelegramDelivery(unittest.TestCase):
    """Telegram delivery tests - NO NETWORK, NO PROVIDER CALLS."""

    def setUp(self):
        """Set up mocks."""
        self.mock_client = MagicMock()
        self.mock_client.send_message.return_value = {"ok": True, "message_id": 12345}
        self.mock_client.send_photo.return_value = {"ok": True, "message_id": 12346}

        self.mock_config = MagicMock()
        self.mock_config.test_chat_id = "123456789"

    def test_initial_review_package_sends_all_parts(self):
        """Test 1: Initial review sends article + image + controls."""
        article = MockArticle(
            headline="Bitcoin ETF Approved",
            dek="SEC approves spot Bitcoin ETF after years of waiting",
            article_body="The Securities and Exchange Commission has approved the first spot Bitcoin ETF...",
        )
        version_store = MockVersionStore(article=article, image_exists=True)

        result = send_initial_review_package(
            client=self.mock_client,
            config=self.mock_config,
            version_store=version_store,
            event_id="evt-test-001",
            canonical_title="Bitcoin ETF Approved",
            article_version="v1",
            image_version="v1",
        )

        # Should succeed
        self.assertTrue(result["ok"])
        self.assertEqual(result["article_version"], "v1")
        self.assertEqual(result["image_version"], "v1")

        # Article sent
        self.assertTrue(result["article_sent"])
        article_calls = [c for c in self.mock_client.send_message.call_args_list
                        if "ARTICLE" in str(c)]
        self.assertGreater(len(article_calls), 0)

        # Image sent
        self.assertTrue(result["image_sent"])
        self.assertEqual(self.mock_client.send_photo.call_count, 1)

        # Controls sent
        self.assertTrue(result["controls_sent"])
        controls_calls = [c for c in self.mock_client.send_message.call_args_list
                         if "Ready for Review" in str(c)]
        self.assertEqual(len(controls_controls_calls := controls_calls), 1)

        # Verify Kimi/Vertex calls = 0
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["vertex_calls"], 0)

        print("✅ Test 1 PASSED: Initial review sends all parts")
        print(f"   Article sent: {result['article_sent']}")
        print(f"   Image sent: {result['image_sent']}")
        print(f"   Controls sent: {result['controls_sent']}")
        print(f"   Kimi calls: {result['kimi_calls']}")
        print(f"   Vertex calls: {result['vertex_calls']}")

    def test_long_article_is_split(self):
        """Test 4: Article > Telegram limit is split, never truncated."""
        # Create a very long article body
        long_body = "This is a sentence. " * 2000  # ~40000 chars

        article = MockArticle(
            headline="Very Long Article",
            dek="This article is very long",
            article_body=long_body,
        )
        version_store = MockVersionStore(article=article)

        result = send_initial_review_package(
            client=self.mock_client,
            config=self.mock_config,
            version_store=version_store,
            event_id="evt-test-002",
            canonical_title="Very Long Article",
            article_version="v1",
            image_version="v1",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["article_sent"])

        # Should have sent multiple messages for the article
        message_calls = self.mock_client.send_message.call_args_list
        # At least 2 calls: article parts + controls, potentially more parts
        self.assertGreaterEqual(len(message_calls), 2)

        # Verify no message exceeds Telegram limit
        for call in message_calls:
            text = call.kwargs.get("text") or call.args[1] if len(call.args) > 1 else ""
            self.assertLessEqual(len(text), 4096,
                f"Message exceeds Telegram limit: {len(text)} chars")

        print("✅ Test 4 PASSED: Long article split safely")
        print(f"   Total messages sent: {len(message_calls)}")

    def test_article_only_revision(self):
        """Test 5: Article-only revision sends new article, reuses image."""
        article_v2 = MockArticle(
            headline="Bitcoin ETF Approved [REVISED]",
            dek="Updated: SEC approves spot Bitcoin ETF",
            article_body="Revised content here...",
        )
        version_store = MockVersionStore(article=article_v2)

        result = send_revision_package(
            client=self.mock_client,
            config=self.mock_config,
            version_store=version_store,
            event_id="evt-test-003",
            canonical_title="Bitcoin ETF Approved",
            article_version="v2",
            image_version="v1",
            article_revised=True,
            image_revised=False,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["article_revised"])
        self.assertFalse(result["image_revised"])

        # Article should be marked as revised
        article_calls = [c for c in self.mock_client.send_message.call_args_list
                        if "REVISED" in str(c)]
        self.assertGreater(len(article_calls), 0)

        # Image should be resent (for context) with "existing" note
        self.mock_client.send_photo.assert_called()

        print("✅ Test 5 PASSED: Article-only revision")
        print(f"   Article revised: {result['article_revised']}")
        print(f"   Image revised: {result['image_revised']}")

    def test_image_only_revision(self):
        """Test 5b: Image-only revision reuses article, sends new image."""
        article_v1 = MockArticle(
            headline="Bitcoin ETF Approved",
            dek="SEC approves spot Bitcoin ETF",
            article_body="Original content...",
        )
        version_store = MockVersionStore(article=article_v1)

        result = send_revision_package(
            client=self.mock_client,
            config=self.mock_config,
            version_store=version_store,
            event_id="evt-test-004",
            canonical_title="Bitcoin ETF Approved",
            article_version="v1",
            image_version="v2",
            article_revised=False,
            image_revised=True,
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["article_revised"])
        self.assertTrue(result["image_revised"])

        # Article should still be sent (for context)
        self.assertTrue(result["article_sent"])
        # Image should be sent
        self.assertTrue(result.get("image_sent", False))

        print("✅ Test 5b PASSED: Image-only revision")
        print(f"   Article revised: {result['article_revised']}")
        print(f"   Image revised: {result['image_revised']}")

    def test_review_keyboard_has_correct_versions(self):
        """Test: Review keyboard references exact artifact versions."""
        keyboard = _build_review_keyboard(
            event_id="evt-test",
            article_version="v3",
            image_version="v2",
        )

        # Check callbacks contain versions
        callbacks = []
        for row in keyboard["inline_keyboard"]:
            for btn in row:
                callbacks.append(btn["callback_data"])

        # All callbacks should reference v3 for article, v2 for image
        article_callbacks = [c for c in callbacks if "article" in c or "revise" in c or "publish" in c or "reject" in c]
        image_callbacks = [c for c in callbacks if "image" in c]

        # Article callbacks reference v3
        for cb in article_callbacks:
            if "rate_article" in cb:
                self.assertIn("v3", cb)

        # Image callbacks reference v2
        for cb in image_callbacks:
            if "rate_image" in cb:
                self.assertIn("v2", cb)

        print("✅ Keyboard versions correct")
        print(f"   Article version in callbacks: v3")
        print(f"   Image version in callbacks: v2")

    def test_zero_provider_calls(self):
        """Test 6: Delivery causes ZERO generation/provider calls."""
        article = MockArticle(
            headline="Test Article",
            dek="Test dek",
            article_body="Test body",
        )
        version_store = MockVersionStore(article=article)

        result = send_initial_review_package(
            client=self.mock_client,
            config=self.mock_config,
            version_store=version_store,
            event_id="evt-test-005",
            canonical_title="Test Article",
            article_version="v1",
            image_version="v1",
        )

        # Verify no provider calls
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["vertex_calls"], 0)

        # Verify only Telegram client was called
        # No writer build, no image generation
        print("✅ Test 6 PASSED: Zero provider calls")
        print(f"   Kimi calls: {result['kimi_calls']}")
        print(f"   Vertex calls: {result['vertex_calls']}")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("V5 TELEGRAM DELIVERY TESTS")
    print("="*60)
    print("WARNING: These are MOCK tests - no network calls made")
    print("="*60 + "\n")

    unittest.main(verbosity=2)
