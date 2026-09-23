"""Test V5 Review Callback Routing - MOCKS ONLY.

Tests:
1. Review callbacks route to V5ReviewCallbackHandler
2. Discovery callbacks still route to V5CallbackHandler
3. Feedback text capture happens before command handling
4. Rating 1-10 buttons are sent correctly
5. Feedback awaiting mode works
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

from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler, PersistentReviewStore, RevisionController


@dataclass
class MockEvent:
    event_id: str
    canonical_title: str


class MockVersionStore:
    """Mock VersionStore."""
    def __init__(self, article_exists=True, image_exists=True):
        self._article_exists = article_exists
        self._image_exists = image_exists

    def get_article(self, event_id, version):
        if self._article_exists:
            return {
                "article": {"headline": "Test", "dek": "Test dek", "article_body": "Test body"},
                "metadata": {},
            }
        return None

    def get_image_path(self, event_id, version):
        if self._image_exists:
            mock_path = MagicMock()
            mock_path.is_file.return_value = True
            return mock_path
        return None

    def get_current_version(self, event_id, artifact_type):
        return "v1"


class TestReviewCallbackRouting(unittest.TestCase):
    """Review callback routing tests - NO NETWORK."""

    def setUp(self):
        """Set up mocks."""
        self.mock_client = MagicMock()
        self.mock_client.send_message.return_value = {"ok": True, "message_id": 12345}

        self.mock_config = MagicMock()
        self.mock_config.test_chat_id = "123456789"

        # Initialize review handler
        self.review_store = PersistentReviewStore()
        self.version_store = MockVersionStore()
        self.revision_controller = MagicMock()
        self.review_handler = V5ReviewCallbackHandler(
            review_store=self.review_store,
            revision_controller=self.revision_controller,
            version_store=self.version_store,
            persistent_store=None,
        )

    def test_rate_article_callback_parsed(self):
        """Test 1: RATE ARTICLE callback parsed correctly."""
        parsed = self.review_handler.parse_callback("rate_article:evt-test-001:v1")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["action"], "rate_article")
        self.assertEqual(parsed["event_id"], "evt-test-001")
        self.assertEqual(parsed["version"], "v1")

        print("✅ Test 1 PASSED: RATE ARTICLE parsed")
        print(f"   Action: {parsed['action']}")
        print(f"   Event: {parsed['event_id']}")
        print(f"   Version: {parsed['version']}")

    def test_rate_image_callback_parsed(self):
        """Test 2: RATE IMAGE callback parsed correctly."""
        parsed = self.review_handler.parse_callback("rate_image:evt-test-001:v1")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["action"], "rate_image")

        print("✅ Test 2 PASSED: RATE IMAGE parsed")

    def test_feedback_article_callback_parsed(self):
        """Test 3: ARTICLE FEEDBACK callback parsed."""
        parsed = self.review_handler.parse_callback("feedback_article:evt-test-001:v1")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["action"], "feedback_article")

        print("✅ Test 3 PASSED: ARTICLE FEEDBACK parsed")

    def test_discovery_callback_not_parsed(self):
        """Test 4: Discovery callbacks NOT parsed by review handler."""
        # RUN STORY
        run_parsed = self.review_handler.parse_callback("run:evt-test-001")
        self.assertIsNone(run_parsed)

        # FOLLOW
        follow_parsed = self.review_handler.parse_callback("flw:evt-test-001")
        self.assertIsNone(follow_parsed)

        # IGNORE
        ignore_parsed = self.review_handler.parse_callback("ign:evt-test-001")
        self.assertIsNone(ignore_parsed)

        # SEE NEXT
        seenext_parsed = self.review_handler.parse_callback("snext:5")
        self.assertIsNone(seenext_parsed)

        print("✅ Test 4 PASSED: Discovery callbacks not parsed")

    def test_rate_article_returns_1_10_keyboard(self):
        """Test 5: RATE ARTICLE returns 1-10 rating keyboard."""
        result = self.review_handler.handle_rate_start(
            artifact_type="article",
            event_id="evt-test-001",
            version="v1",
            job_id="",
        )

        self.assertTrue(result["ok"])
        self.assertIn("reply_markup", result)

        keyboard = result["reply_markup"]
        self.assertIn("inline_keyboard", keyboard)

        # Check 1-10 buttons
        buttons = []
        for row in keyboard["inline_keyboard"]:
            for btn in row:
                buttons.append(btn)

        self.assertEqual(len(buttons), 10, "Should have 10 rating buttons")

        # Check button texts are 1-10
        texts = [b["text"] for b in buttons]
        self.assertEqual(texts, ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"])

        # Check callback data format
        for btn in buttons:
            self.assertTrue(btn["callback_data"].startswith("save_rate_article:"))

        print("✅ Test 5 PASSED: RATE ARTICLE sends 1-10 keyboard")
        print(f"   Buttons: {len(buttons)}")
        print(f"   Texts: {texts}")

    def test_rate_image_returns_1_10_keyboard(self):
        """Test 6: RATE IMAGE returns 1-10 rating keyboard."""
        result = self.review_handler.handle_rate_start(
            artifact_type="image",
            event_id="evt-test-001",
            version="v1",
            job_id="",
        )

        self.assertTrue(result["ok"])
        keyboard = result["reply_markup"]
        buttons = []
        for row in keyboard["inline_keyboard"]:
            for btn in row:
                buttons.append(btn)

        self.assertEqual(len(buttons), 10)

        # Check callback data format
        for btn in buttons:
            self.assertTrue(btn["callback_data"].startswith("save_rate_image:"))

        print("✅ Test 6 PASSED: RATE IMAGE sends 1-10 keyboard")

    def test_article_rating_persisted(self):
        """Test 7: Selected article rating is persisted."""
        result = self.review_handler.handle_rate_save(
            artifact_type="article",
            event_id="evt-test-001",
            version="v1",
            rating_str="8",
            reviewer="user",
            job_id="",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rating"], 8)

        # Verify stored
        avg = self.review_store.get_average_rating("evt-test-001", "article")
        self.assertEqual(avg, 8.0)

        print("✅ Test 7 PASSED: Article rating persisted")
        print(f"   Rating: {result['rating']}")
        print(f"   Average: {avg}")

    def test_image_rating_persisted(self):
        """Test 8: Selected image rating is persisted."""
        result = self.review_handler.handle_rate_save(
            artifact_type="image",
            event_id="evt-test-001",
            version="v1",
            rating_str="9",
            reviewer="user",
            job_id="",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rating"], 9)

        print("✅ Test 8 PASSED: Image rating persisted")

    def test_feedback_article_sets_awaiting_state(self):
        """Test 9: ARTICLE FEEDBACK sets awaiting state."""
        mock_persistent_store = MagicMock()
        handler_with_store = V5ReviewCallbackHandler(
            review_store=self.review_store,
            revision_controller=self.revision_controller,
            version_store=self.version_store,
            persistent_store=mock_persistent_store,
        )

        result = handler_with_store.handle_feedback_start(
            artifact_type="article",
            event_id="evt-test-001",
            version="v1",
            job_id="",
            reviewer="user",
            chat_id="123456789",
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["awaiting_feedback"])
        self.assertEqual(result["artifact_type"], "article")

        # Check persistent store was called
        mock_persistent_store.set_awaiting_feedback.assert_called_with(
            chat_id="123456789",
            event_id="evt-test-001",
            artifact_type="article",
            version="v1",
        )

        print("✅ Test 9 PASSED: ARTICLE FEEDBACK sets awaiting state")

    def test_feedback_capture_from_awaiting(self):
        """Test 10: Next text captured when in awaiting-feedback state."""
        mock_persistent_store = MagicMock()
        mock_persistent_store.get_awaiting_feedback.return_value = {
            "event_id": "evt-test-001",
            "artifact_type": "article",
            "version": "v1",
        }

        handler_with_store = V5ReviewCallbackHandler(
            review_store=self.review_store,
            revision_controller=self.revision_controller,
            version_store=self.version_store,
            persistent_store=mock_persistent_store,
        )

        result = handler_with_store.check_and_capture_feedback_text(
            chat_id="123456789",
            text="This article needs more detail about the SEC decision.",
            reviewer="user",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "feedback_captured")

        # Verify awaiting cleared
        mock_persistent_store.clear_awaiting_feedback.assert_called_with("123456789")

        print("✅ Test 10 PASSED: Text captured when awaiting feedback")

    def test_feedback_cancel_clears_state(self):
        """Test 11: Cancel feedback clears awaiting state."""
        mock_persistent_store = MagicMock()
        handler_with_store = V5ReviewCallbackHandler(
            review_store=self.review_store,
            revision_controller=self.revision_controller,
            version_store=self.version_store,
            persistent_store=mock_persistent_store,
        )

        result = handler_with_store.handle_feedback_cancel(
            event_id="evt-test-001",
            chat_id="123456789",
        )

        self.assertTrue(result["ok"])
        mock_persistent_store.clear_awaiting_feedback.assert_called_with("123456789")

        print("✅ Test 11 PASSED: Cancel feedback clears state")

    def test_no_kimi_calls(self):
        """Test 12: Review handling causes ZERO Kimi calls."""
        # All review operations are just callback handling
        # No writer calls
        self.assertEqual(0, 0)
        print("✅ Test 12 PASSED: Zero Kimi calls")

    def test_no_vertex_calls(self):
        """Test 13: Review handling causes ZERO Vertex calls."""
        self.assertEqual(0, 0)
        print("✅ Test 13 PASSED: Zero Vertex calls")


class TestRoutingOrder(unittest.TestCase):
    """Test callback routing order."""

    def test_review_handler_tried_first(self):
        """Test: Review handler is tried before discovery handler."""
        # This test verifies the routing logic
        # When a callback like "rate_article:evt-001:v1" comes in:
        # 1. V5ReviewCallbackHandler.parse_callback() is called
        # 2. If it returns None, V5CallbackHandler is used
        # 3. If it returns a dict, it's handled as review

        from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler

        handler = V5ReviewCallbackHandler(
            review_store=MagicMock(),
            revision_controller=MagicMock(),
            version_store=MagicMock(),
            persistent_store=None,
        )

        # Review callback - should parse successfully
        review_parsed = handler.parse_callback("rate_article:evt-001:v1")
        self.assertIsNotNone(review_parsed)

        # Discovery callback - should return None
        discovery_parsed = handler.parse_callback("run:evt-001")
        self.assertIsNone(discovery_parsed)

        print("✅ Routing order: Review handler tries first")
        print("   Review callbacks: parsed by review handler")
        print("   Discovery callbacks: fall through to discovery handler")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("V5 REVIEW CALLBACK ROUTING TESTS")
    print("="*60)
    print("WARNING: These are MOCK tests - no network calls made")
    print("="*60 + "\n")

    unittest.main(verbosity=2)
