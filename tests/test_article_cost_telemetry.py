"""Offline tests for article cost telemetry. ZERO provider calls."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.writer.v4.article_cost_telemetry import (
    UNVERIFIED,
    build_article_cost_record,
    build_batch_cost_report,
    report_batch_from_attempts_root,
    resolve_verified_kimi_pricing,
)


class ArticleCostTelemetryTests(unittest.TestCase):
    def test_unverified_pricing_by_default(self) -> None:
        pricing = resolve_verified_kimi_pricing({})
        self.assertFalse(pricing["pricing_verified"])
        row = build_article_cost_record(
            batch_id="b1",
            event_id="event-1",
            article={"headline": "H", "article_body": "one two three"},
            diagnostic={"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            publishable=True,
            environ={},
        )
        self.assertEqual(row["kimi_input_tokens"], 10)
        self.assertEqual(row["kimi_output_tokens"], 5)
        self.assertEqual(row["kimi_total_tokens"], 15)
        self.assertEqual(row["article_generation_cost_usd"], UNVERIFIED)
        self.assertEqual(row["article_generation_cost_inr"], UNVERIFIED)
        self.assertFalse(row["pricing_verified"])

    def test_verified_env_pricing(self) -> None:
        env = {
            "NEWSAGENT_V2_KIMI_PRICING_VERIFIED": "true",
            "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK": "2.0",
            "NEWSAGENT_V2_USD_TO_INR": "80",
            "NEWSAGENT_V2_KIMI_PRICING_SOURCE": "unit_test_config",
        }
        row = build_article_cost_record(
            batch_id="b1",
            event_id="event-1",
            article={"headline": "H", "article_body": "alpha beta"},
            diagnostic={"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}},
            publishable=True,
            environ=env,
        )
        self.assertTrue(row["pricing_verified"])
        self.assertEqual(row["article_generation_cost_usd"], 3.0)
        self.assertEqual(row["article_generation_cost_inr"], 240.0)

    def test_batch_report_marks_money_unverified(self) -> None:
        rows = [
            build_article_cost_record(
                batch_id="b1",
                event_id="ok-1",
                diagnostic={"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
                publishable=True,
                environ={},
            ),
            build_article_cost_record(
                batch_id="b1",
                event_id="fail-1",
                diagnostic={"usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}},
                publishable=False,
                environ={},
            ),
        ]
        batch = build_batch_cost_report(batch_id="b1", article_rows=rows, environ={})
        self.assertEqual(batch["articles_generated"], 2)
        self.assertEqual(batch["publishable_articles"], 1)
        self.assertEqual(batch["kimi_input_tokens"], 140)
        self.assertEqual(batch["kimi_output_tokens"], 60)
        self.assertEqual(batch["successful_article_tokens"], 150)
        self.assertEqual(batch["failed_candidate_tokens"], 50)
        self.assertEqual(batch["total_article_generation_cost_usd"], UNVERIFIED)
        self.assertEqual(batch["effective_cost_per_publishable_article_usd"], UNVERIFIED)
        self.assertFalse(batch["pricing_verified"])

    def test_report_from_disk_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "attempts" / "event-9"
            root.mkdir(parents=True)
            (root / "writer_request_diagnostic.json").write_text(
                json.dumps({"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}),
                encoding="utf-8",
            )
            (root / "attempt.json").write_text(
                json.dumps({"ok": True, "final_word_count": 12, "depth": {"article_type": "STANDARD_BRIEF"}}),
                encoding="utf-8",
            )
            (root / "article.json").write_text(
                json.dumps({"headline": "Disk", "article_body": "a b c d e f g h i j k l"}),
                encoding="utf-8",
            )
            report = report_batch_from_attempts_root(
                Path(tmp) / "attempts",
                batch_id="disk-batch",
                environ={},
                publishable_event_ids={"event-9"},
            )
            self.assertEqual(report["kimi_total_tokens"], 18)
            self.assertEqual(report["publishable_articles"], 1)
            self.assertEqual(report["total_article_generation_cost_usd"], UNVERIFIED)


if __name__ == "__main__":
    unittest.main()


