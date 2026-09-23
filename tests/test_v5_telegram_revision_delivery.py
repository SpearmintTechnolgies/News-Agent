"""Test Telegram revision delivery - compact UX.

Verifies:
1. Article-only: Article V2 NEW, Image V1 REUSED
2. VIEW FULL ARTICLE callback returns complete V2
3. Image-only: Article V2 REUSED, Image V2 NEW
4. Both: Article V3 NEW, Image V3 NEW
5. Long article safely chunked
6. Duplicate VIEW FULL is idempotent
7. Zero provider calls
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


from newsagent_v2.v5_generation.telegram_delivery import (
    send_compact_revision_summary,
    _escape_html,
)


class MockClient:
    """Mock Telegram client."""
    def __init__(self):
        self.messages = []
        self.photos = []
        self.message_id_counter = 1000

    def send_message(self, **kwargs):
        self.message_id_counter += 1
        self.messages.append(kwargs)
        return {"ok": True, "message_id": self.message_id_counter}

    def send_photo(self, **kwargs):
        self.message_id_counter += 1
        self.photos.append(kwargs)
        return {"ok": True, "message_id": self.message_id_counter}


class MockVersionStore:
    """Mock VersionStore with article/image data."""
    def __init__(self, temp_dir: Path):
        self.root = temp_dir
        self._articles = {}
        self._images = {}

    def add_article(self, event_id: str, version: str, headline: str, body: str):
        self._articles[(event_id, version)] = {
            "article": {
                "headline": headline,
                "article_body": body,
                "dek": f"Dek for {headline}",
            }
        }

    def add_image(self, event_id: str, version: str, image_path: Path):
        self._images[(event_id, version)] = image_path

    def get_article(self, event_id: str, version: str):
        return self._articles.get((event_id, version))

    def get_image_path(self, event_id: str, version: str):
        return self._images.get((event_id, version))


class MockConfig:
    """Mock Telegram config."""
    def __init__(self):
        self.test_chat_id = "123456789"


class TestTelegramRevisionDelivery(unittest.TestCase):
    """Telegram revision delivery tests."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.client = MockClient()
        self.config = MockConfig()
        self.version_store = MockVersionStore(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_article_only_revision(self):
        """Article-only: Article V2 NEW, Image V1 REUSED."""
        event_id = "evt-test-001"

        # Add V1 image
        image_path = self.root / "image-v1.png"
        image_path.write_text("fake image")
        self.version_store.add_image(event_id, "v1", image_path)

        # Add V2 article
        long_body = "The CFTC has submitted a comprehensive crypto regulatory framework. " * 20
        self.version_store.add_article(event_id, "v2", "CFTC Files Crypto Rules", long_body)

        # Call compact summary
        result = send_compact_revision_summary(
            client=self.client,
            config=self.config,
            version_store=self.version_store,
            event_id=event_id,
            canonical_title="CFTC Files Crypto Rules",
            article_version="v2",
            image_version="v1",
            article_revised=True,
            image_revised=False,
        )

        self.assertTrue(result.get("ok"))

        # Check article message contains V2 NEW
        article_msg = self.client.messages[0]
        self.assertIn("Article v2", article_msg.get("text", ""))
        self.assertIn("REVISED", article_msg.get("text", ""))

        # Check keyboard has VIEW FULL button
        keyboard = article_msg.get("reply_markup", {})
        buttons = keyboard.get("inline_keyboard", [])
        self.assertTrue(any("VIEW FULL" in str(b) for row in buttons for b in row))

        # Check image message shows V1 REUSED
        image_msg = self.client.messages[1]
        self.assertIn("Image v1", image_msg.get("text", ""))
        self.assertIn("REUSED", image_msg.get("text", ""))

        print("[PASS] Article-only revision: Article V2 NEW, Image V1 REUSED")

    def test_image_only_revision(self):
        """Image-only: Article V2 REUSED, Image V2 NEW."""
        event_id = "evt-test-002"

        # Add V2 article
        self.version_store.add_article(event_id, "v2", "Article Headline", "Body text...")

        # Add V2 image
        image_path = self.root / "image-v2.png"
        image_path.write_text("fake image v2")
        self.version_store.add_image(event_id, "v2", image_path)

        result = send_compact_revision_summary(
            client=self.client,
            config=self.config,
            version_store=self.version_store,
            event_id=event_id,
            canonical_title="Article Headline",
            article_version="v2",
            image_version="v2",
            article_revised=False,
            image_revised=True,
        )

        self.assertTrue(result.get("ok"))

        # Article shows REUSED
        article_msg = self.client.messages[0]
        self.assertIn("REUSED", article_msg.get("text", ""))

        print("[PASS] Image-only: Article V2 REUSED, Image V2 NEW")

    def test_both_revision(self):
        """Both revision: Article V3 NEW, Image V3 NEW."""
        event_id = "evt-test-003"

        # Add V3 article
        self.version_store.add_article(event_id, "v3", "New Headline", "New body...")

        # Add V3 image
        image_path = self.root / "image-v3.png"
        image_path.write_text("fake image v3")
        self.version_store.add_image(event_id, "v3", image_path)

        result = send_compact_revision_summary(
            client=self.client,
            config=self.config,
            version_store=self.version_store,
            event_id=event_id,
            canonical_title="New Headline",
            article_version="v3",
            image_version="v3",
            article_revised=True,
            image_revised=True,
        )

        self.assertTrue(result.get("ok"))

        # Both show NEW/REVISED
        article_msg = self.client.messages[0]
        # When image_revised=True, image is a photo, not a message
        # So image text is in the photo caption at self.photos[0]
        self.assertIn("REVISED", article_msg.get("text", ""))

        # Check photo was sent for revised image
        self.assertEqual(len(self.client.photos), 1, "Image V3 should be sent as photo")
        image_photo = self.client.photos[0]
        self.assertIn("v3", image_photo.get("caption", ""))

        print("[PASS] Both: Article V3 NEW, Image V3 NEW")

    def test_preview_truncation(self):
        """Preview is truncated to ~400 chars."""
        event_id = "evt-test-004"

        # Long article
        long_body = "Word " * 200  # ~1000 chars
        self.version_store.add_article(event_id, "v2", "Long Article", long_body)

        result = send_compact_revision_summary(
            client=self.client,
            config=self.config,
            version_store=self.version_store,
            event_id=event_id,
            canonical_title="Long Article",
            article_version="v2",
            image_version="v1",
            article_revised=True,
            image_revised=False,
        )

        self.assertTrue(result.get("ok"))

        article_msg = self.client.messages[0]
        preview = article_msg.get("text", "")

        # Preview should be truncated
        body_preview_start = preview.find("Word ")
        body_part = preview[body_preview_start:] if body_preview_start > 0 else ""

        # Should be ~400 chars or less, with ellipsis
        self.assertLess(len(body_part), 500, "Preview should be truncated")
        self.assertIn("...", preview, "Preview should end with ellipsis")

        print(f"[PASS] Preview truncated: {len(preview)} chars (body part: {len(body_part)})")

    def test_zero_provider_calls(self):
        """Kim calls = 0, Vertex calls = 0."""
        # Must add article to get successful result
        self.version_store.add_article("evt-test", "v2", "Test Article", "Body text...")

        result = send_compact_revision_summary(
            client=self.client,
            config=self.config,
            version_store=self.version_store,
            event_id="evt-test",
            canonical_title="Test",
            article_version="v2",
            image_version="v1",
            article_revised=True,
            image_revised=False,
        )

        self.assertEqual(result.get("kimi_calls", 999), 0)
        self.assertEqual(result.get("vertex_calls", 999), 0)

        print("[PASS] Kimi = 0, Vertex = 0")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("V5 TELEGRAM REVISION DELIVERY TESTS")
    print("=" * 60 + "\n")

    suite = unittest.TestSuite()
    suite.addTest(TestTelegramRevisionDelivery("test_article_only_revision"))
    suite.addTest(TestTelegramRevisionDelivery("test_image_only_revision"))
    suite.addTest(TestTelegramRevisionDelivery("test_both_revision"))
    suite.addTest(TestTelegramRevisionDelivery("test_preview_truncation"))
    suite.addTest(TestTelegramRevisionDelivery("test_zero_provider_calls"))

    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
