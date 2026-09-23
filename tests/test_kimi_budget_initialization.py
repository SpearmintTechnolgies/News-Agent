"""Regression for new-story Kimi budget initialization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from newsagent_v2.providers.kimi_budget import KimiBudgetError, KimiBudgetManager, KimiBudgetStore
from newsagent_v2.providers.kimi_guard import KimiGuard


class TestKimiBudgetInitialization(unittest.TestCase):
    def test_new_story_initializes_and_reserves_with_limits(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = KimiBudgetManager(KimiBudgetStore(Path(root)))
            event_id = "evt-budget-init-regression"

            budget = manager.store.create_for_new_story(event_id)
            self.assertEqual(budget.request_count, 0)

            guard = KimiGuard(event_id=event_id, manager=manager)
            result = guard.check_before_request(
                [{"role": "user", "content": "offline regression"}],
                500,
                "initial_writer",
            )
            self.assertTrue(result.allowed)

            guard.reconcile_after_request(
                success=True,
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            )

            summary = manager.build_story_usage_summary(event_id)
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["total_tokens"], 150)

            with self.assertRaises(KimiBudgetError) as context:
                manager.check_and_reserve(
                    event_id=event_id,
                    stage="oversized_request",
                    estimated_prompt=20_000,
                    requested_completion=500,
                )
            self.assertEqual(context.exception.reason, "MAX_PROMPT_TOKENS_PER_REQUEST")


if __name__ == "__main__":
    unittest.main()