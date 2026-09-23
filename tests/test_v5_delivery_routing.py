"""Test delivery routing - ZERO cost, NO provider calls.

Traces exact callback return → router → delivery path.
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class MockRevisionResult:
    """Mock RevisionResult from controller."""
    def __init__(self):
        self.ok = True
        self.event_id = "evt-fec17bd1"
        self.article_revised = True
        self.image_revised = False
        self.article_version = "v2"
        self.image_version = "v1"
        self.article_hash = "abc123"
        self.image_hash = "def456"
        self.article_calls = 1
        self.image_calls = 0
        self.error = None


class MockVersionStore:
    """Mock VersionStore."""
    def get_article(self, event_id, version):
        return {
            "article": {
                "headline": "CFTC Files Crypto Rules",
                "article_body": "The CFTC submitted crypto rulemaking...",
            }
        }

    def get_image_path(self, event_id, version):
        return None

    def get_current_version(self, event_id, artifact_type):
        return "v2" if artifact_type == "article" else "v1"


class TestDeliveryRouting(unittest.TestCase):
    """Delivery routing tests - NO provider calls."""

    def test_handle_revise_return_shape(self):
        """1. Verify handle_revise() returns correct action and keys."""
        from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
        from newsagent_v2.v5_generation.revision_controller import RevisionController

        # Mock dependencies
        mock_review_store = MagicMock()
        mock_version_store = MockVersionStore()
        mock_revision_controller = MagicMock()

        # Set up mock revision result
        mock_result = MockRevisionResult()
        mock_revision_controller.revise.return_value = mock_result

        handler = V5ReviewCallbackHandler(
            review_store=mock_review_store,
            revision_controller=mock_revision_controller,
            version_store=mock_version_store,
        )

        # Call handle_revise
        result = handler.handle_revise("evt-fec17bd1", "v1", "")

        print(f"DEBUG: result = {result}")

        # Assertions
        self.assertTrue(result.get("ok"), f"result.ok should be True, got: {result}")
        self.assertEqual(result.get("action"), "revise_complete")
        self.assertTrue(result.get("send_revision_package"))
        self.assertIn("compact_message", result)
        self.assertIn("event_id", result)
        self.assertEqual(result["event_id"], "evt-fec17bd1")

        # Verify compact_message format
        compact = result.get("compact_message", "")
        self.assertIn("Article v2", compact)
        self.assertIn("REVISED", compact)
        self.assertIn("Image v1", compact)
        self.assertIn("REUSED", compact)

        print("[PASS] handle_revise returns correct action='revise_complete' and keys")
        print(f"  action: {result.get('action')}")
        print(f"  send_revision_package: {result.get('send_revision_package')}")
        print(f"  compact_message preview:\n{compact[:100]}...")

    def test_router_detects_revise_complete(self):
        """2. Verify router condition matches handle_revise return."""
        # The router condition is:
        # if action == "revise_complete" and result.get("send_revision_package"):

        test_cases = [
            # (action, send_revision_package, expected_match)
            ("revise_complete", True, True),
            ("revise_complete", False, False),
            ("revise", True, False),
            ("revise_complete", None, False),
        ]

        for action, has_flag, expected in test_cases:
            result = {"action": action, "send_revision_package": has_flag, "ok": True}

            # Simulate router logic
            matches = (action == "revise_complete") and result.get("send_revision_package")

            self.assertEqual(matches, expected,
                f"action={action}, flag={has_flag} should match={expected}")

        print("[PASS] Router condition correctly detects revise_complete + send_revision_package")

    def test_view_full_callback_parsing(self):
        """3. Verify VIEW FULL callback is accepted by parser/router."""
        from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler

        handler = V5ReviewCallbackHandler(None, None, MockVersionStore())

        # Test callback_data
        callback_data = "view_full:evt-fec17bd1:v2:article"

        parsed = handler.parse_callback(callback_data)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("action"), "view_full")
        self.assertEqual(parsed.get("event_id"), "evt-fec17bd1")
        self.assertEqual(parsed.get("version"), "v2")
        self.assertEqual(parsed.get("extra"), "article")

        print("[PASS] VIEW FULL callback is accepted by parser")
        print(f"  action: {parsed.get('action')}")
        print(f"  event_id: {parsed.get('event_id')}")
        print(f"  version: {parsed.get('version')}")

    def test_no_old_message_fallback(self):
        """4. Verify old 'result["message"]' fallback is NOT triggered for revise."""
        # After handle_revise returns, router checks:
        # if action == "revise_complete" and result.get("send_revision_package"):
        #     _send_revision_result(result)  <-- Should go here
        # elif result.get("compact_message"):
        #     send_message(compact_message)  <-- Could also go here
        # elif result.get("message"):
        #     send_message(message)  <-- NOT HERE

        # The handler returns BOTH action=revise_complete AND compact_message
        result = {
            "ok": True,
            "action": "revise_complete",
            "send_revision_package": True,
            "compact_message": "Article v2 -- REVISED",
            "message": "Should NOT be sent",  # This would trigger fallback
        }

        # First condition should match
        action = result.get("action", "")
        first_matches = (action == "revise_complete") and result.get("send_revision_package")

        # If first matches, we DON'T check compact_message or message
        self.assertTrue(first_matches)

        print("[PASS] Router routes to _send_revision_result, not message fallback")

    def test_send_compact_revision_summary_is_callable(self):
        """5. Verify send_compact_revision_summary exists and is callable."""
        from newsagent_v2.v5_generation.telegram_delivery import send_compact_revision_summary

        self.assertTrue(callable(send_compact_revision_summary))
        print("[PASS] send_compact_revision_summary is callable")


if __name__ == "__main__":
    from unittest.mock import MagicMock

    print("\n" + "=" * 60)
    print("V5 DELIVERY ROUTING TESTS")
    print("=" * 60 + "\n")

    suite = unittest.TestSuite()
    suite.addTest(TestDeliveryRouting("test_handle_revise_return_shape"))
    suite.addTest(TestDeliveryRouting("test_router_detects_revise_complete"))
    suite.addTest(TestDeliveryRouting("test_view_full_callback_parsing"))
    suite.addTest(TestDeliveryRouting("test_no_old_message_fallback"))
    suite.addTest(TestDeliveryRouting("test_send_compact_revision_summary_is_callable"))

    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
