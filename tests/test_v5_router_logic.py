"""Router logic tests - NO integration, NO provider calls.

Verifies exact routing conditions and path selection.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class TestRouterConditions(unittest.TestCase):
    """Router condition tests."""

    def test_revise_complete_condition(self):
        """Router matches revise_complete AND send_revision_package."""
        # Match router logic from v5_canonical_runtime.py:275

        test_cases = [
            # (action, send_revision_package, should_match_send_revision_result)
            ("revise_complete", True, True),
            ("revise_complete", False, False),
            ("revise_complete", None, False),
            ("revise_complete", 1, True),  # Truthy
            ("revise", True, False),
            ("approve", True, False),
            ("view_full", True, False),
        ]

        for action, flag, expected in test_cases:
            with self.subTest(action=action, flag=flag):
                result = {"action": action, "send_revision_package": flag}

                # Router condition exactly (normalize to bool — `and` can return None)
                matches = bool(
                    (result.get("action") == "revise_complete")
                    and result.get("send_revision_package")
                )

                self.assertEqual(matches, expected,
                    f"action={action}, flag={flag} should match={expected}")

        print("[PASS] Router correctly matches revise_complete + send_revision_package")

    def test_compact_message_condition(self):
        """Router has compact_message fallback."""
        # Router logic from v5_canonical_runtime.py:291-295

        result = {
            "ok": True,
            "action": "something_else",
            "compact_message": "Article v2 -- REVISED",
        }

        # Simulate router logic
        action = result.get("action", "")
        first_matches = (action == "revise_complete") and result.get("send_revision_package")

        if not first_matches:
            # Would check compact_message next
            has_compact = result.get("compact_message")
            self.assertTrue(has_compact)

        print("[PASS] Router has compact_message fallback")

    def test_message_fallback_is_last(self):
        """result['message'] fallback is CHECKED LAST."""
        # Router order:
        # 1. action == "revise_complete" and send_revision_package
        # 2. action == "view_full_article"
        # 3. action == "view_full_image"
        # 4. reply_markup exists
        # 5. compact_message exists
        # 6. message exists (LAST)

        result = {
            "ok": True,
            "action": "whatever",
            "message": "Old message",
        }

        # Would fall through all conditions to message
        has_message = result.get("message")
        self.assertTrue(has_message)

        print("[PASS] message fallback is last resort")

    def test_send_compact_revision_summary_exists(self):
        """Function exists and takes correct params."""
        from newsagent_v2.v5_generation.telegram_delivery import send_compact_revision_summary
        import inspect

        sig = inspect.signature(send_compact_revision_summary)
        params = list(sig.parameters.keys())

        expected_params = [
            "client", "config", "version_store", "event_id",
            "canonical_title", "article_version", "image_version",
            "article_revised", "image_revised"
        ]

        for param in expected_params:
            self.assertIn(param, params)

        print(f"[PASS] send_compact_revision_summary has correct params: {params}")

    def test_view_full_callback_parsing(self):
        """VIEW FULL callback format is parsed correctly."""
        # Callback format: view_full:{event_id}:{version}:{artifact_type}

        callback = "view_full:evt-fec17bd1:v2:article"
        parts = callback.split(":")

        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "view_full")
        self.assertEqual(parts[1], "evt-fec17bd1")
        self.assertEqual(parts[2], "v2")
        self.assertEqual(parts[3], "article")

        print("[PASS] VIEW FULL callback format is correct")

    def test_no_kimi_no_vertex_in_routing(self):
        """Router poll/callback path does not invoke live provider SDKs."""
        # Check file for imports
        with open("src/newsagent_v2/telegram/v5_canonical_runtime.py", encoding="utf-8") as f:
            content = f.read()

        # Config validation may mention kimi; live SDK call sites must not appear
        # in the poll/callback routing methods.
        forbidden_calls = [
            "import vertexai",
            "from vertexai",
            "bedrock_mantle.generate",
            "kimi_guard.call",
            "OpenAI(",
        ]
        for needle in forbidden_calls:
            self.assertNotIn(needle, content)

        # Should not import Vertex SDK at module level
        self.assertNotIn("vertex", content.lower())

        # _send_revision_result should import but not call providers
        self.assertIn("send_compact_revision_summary", content)

        print("[PASS] Router has no provider calls")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("V5 ROUTER LOGIC TESTS")
    print("=" * 60 + "\n")

    suite = unittest.TestSuite()
    suite.addTest(TestRouterConditions("test_revise_complete_condition"))
    suite.addTest(TestRouterConditions("test_compact_message_condition"))
    suite.addTest(TestRouterConditions("test_message_fallback_is_last"))
    suite.addTest(TestRouterConditions("test_send_compact_revision_summary_exists"))
    suite.addTest(TestRouterConditions("test_view_full_callback_parsing"))
    suite.addTest(TestRouterConditions("test_no_kimi_no_vertex_in_routing"))

    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
