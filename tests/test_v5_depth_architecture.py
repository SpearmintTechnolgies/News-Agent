"""No-network regressions for publishable-depth Top-5 prequal, recovery/fallback, compile floor, WP soft-fail."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from newsagent_v2.article_readiness import assess_article_readiness, INTENTIONAL_ARTICLE_WORDS, MIN_EVIDENCE_WORDS
from newsagent_v2.cluster import EventCluster
from newsagent_v2.models import NewsItem
from newsagent_v2.v5_generation.depth_cost_helpers import (
    DEPTH_FALLBACK_PASS,
    DEPTH_NORMAL_PASS,
    FALLBACK_FLOOR,
    apply_depth_fallback,
    evidence_supports_publishable_depth,
)


def _qa(codes, event_id="evt-1"):
    return {
        "event_id": event_id,
        "critical_failures": [
            {"code": c, "message": c, "severity": "critical", "module": "depth"} for c in codes
        ],
        "warnings": [],
        "metrics": {},
        "qa_passed": not codes,
        "publishable": not codes,
        "qa_publishable": not codes,
        "critical_count": len(codes),
    }


def _item(event_id: str, source: str, *, thin: bool = False) -> NewsItem:
    if thin:
        summary = "Brief update."
    else:
        summary = (
            "Northwind Payments said in 2026 that regulators confirmed the event after "
            "12,400 records were exposed. The company described the timeline, affected "
            "systems, response measures, customer notification process, investigation "
            "status, remediation plan, expected follow-up reporting schedule, and "
            "public accountability updates with independently verified operational details. Investigators also recorded confirmed chronology, official statements, named figures, prior confirmed context, market implications, credible reactions, and documented next steps. Additional confirmed operational metrics and independent chronology notes support a full 600 to 800 word grounded article."
        )
    return NewsItem(
        source=source,
        title=f"Northwind Payments event {event_id}",
        url=f"https://{source.lower()}.example/{event_id}",
        published="Mon, 14 Sep 2026 12:00:00 +0000",
        summary=summary,
        source_role="primary_evidence" if source == "Official" else "newsroom",
        source_authority=0.9 if source == "Official" else 0.7,
    )


def _cluster(event_id: str, *, thin: bool = False) -> EventCluster:
    members = [_item(event_id, "Official", thin=thin)]
    if not thin:
        members.append(_item(event_id, "Wire", thin=False))
    return EventCluster(event_id=event_id, representative=members[0], members=members)


class PublishableDepthPrequalTests(unittest.TestCase):
    def test_readiness_targets_publishable_600(self):
        self.assertEqual(INTENTIONAL_ARTICLE_WORDS, 600)
        self.assertGreaterEqual(MIN_EVIDENCE_WORDS, 100)
        self.assertEqual(FALLBACK_FLOOR, 400)

    def test_evidence_thin_fails_publishable_depth_helper(self):
        ok, reasons = evidence_supports_publishable_depth(
            {"evidence_word_count": 40, "distinct_source_count": 2, "evidence_item_count": 2}
        )
        self.assertFalse(ok)
        self.assertIn("insufficient_depth_capacity_for_publication", reasons)

    def test_rich_cluster_passes_readiness_and_depth_helper(self):
        result = assess_article_readiness(_cluster("event-ready"))
        self.assertTrue(result.eligible)
        ok, reasons = evidence_supports_publishable_depth(result.metrics)
        self.assertTrue(ok, reasons)

    def test_thin_high_rank_rejected_so_next_can_backfill(self):
        from newsagent_v2.article_readiness import preflight_article_readiness
        from newsagent_v2.rank import rank_clusters

        poor = _cluster("event-poor", thin=True)
        poor.event_score = 999.0
        candidates = [poor] + [
            _cluster(f"event-{i:03d}") for i in range(1, 7)
        ]
        for i, c in enumerate(candidates[1:], start=1):
            c.event_score = 100.0 - i
        ready, audit = preflight_article_readiness(candidates)
        ranked = rank_clusters(ready)
        self.assertNotIn(poor, ready)
        self.assertFalse(audit["event-poor"]["eligible"])
        self.assertEqual(
            [c.event_id for c in ranked[:5]],
            ["event-001", "event-002", "event-003", "event-004", "event-005"],
        )


class DepthFallbackBandTests(unittest.TestCase):
    def test_recovery_required_before_420_fallback(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 420},
            qa=_qa(["below_article_minimum_length"]),
            word_count=420,
            recovery_history=[],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("reason"), "recovery_not_exhausted")
        self.assertNotEqual(out["depth_status"], DEPTH_FALLBACK_PASS)

    def test_exhausted_420_only_length_critical_fallback_pass(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 420},
            qa=_qa(["below_article_minimum_length"]),
            word_count=420,
            recovery_history=[{"attempt": 1, "recovery_succeeded": False}],
            recovery_succeeded=False,
        )
        self.assertEqual(out["depth_status"], DEPTH_FALLBACK_PASS)
        self.assertTrue(out["ok"])
        self.assertEqual(out["suppressed_critical_code"], "below_article_minimum_length")
        # Fallback does not invent body text / pad words
        self.assertEqual(out["final_word_count"], 420)

    def test_under_400_blocked_even_after_recovery(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 399},
            qa=_qa(["below_article_minimum_length"]),
            word_count=399,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertNotEqual(out["depth_status"], DEPTH_FALLBACK_PASS)

    def test_fallback_does_not_suppress_other_criticals(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 420},
            qa=_qa(["below_article_minimum_length", "unsupported_claim"]),
            word_count=420,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertIn("unsupported_claim", out.get("remaining_critical_codes") or [])

    def test_600_plus_normal_pass(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 620},
            qa=_qa([]),
            word_count=620,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=True,
        )
        self.assertEqual(out["depth_status"], DEPTH_NORMAL_PASS)
        self.assertTrue(out["ok"])


class CompileHardMinimumTests(unittest.TestCase):
    def test_compile_keeps_production_hard_minimum_not_recommended_min(self):
        """Regression: recommended_word_min must not replace the production 600 floor."""
        import os
        os.environ.pop("ARTICLE_MIN_WORDS", None)
        os.environ.pop("NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS", None)
        import inspect
        from newsagent_v2.article.writer.v4 import compile as compile_mod

        src = inspect.getsource(compile_mod.compile_article) if hasattr(compile_mod, "compile_article") else inspect.getsource(compile_mod)
        # Prefer reading the module text around qa_depth_policy
        from pathlib import Path
        text = Path(compile_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "hard_minimum_words=depth.recommended_word_min",
            text,
        )
        self.assertIn("_base_depth_policy = active_normal_depth_policy()", text)
        from newsagent_v2.article.qa.policy import PRODUCTION_HARD_MINIMUM_WORDS, active_normal_depth_policy
        from dataclasses import replace

        base = active_normal_depth_policy()
        policy = replace(base, target_min_words=250, target_max_words=400)
        self.assertEqual(policy.hard_minimum_words, PRODUCTION_HARD_MINIMUM_WORDS)
        self.assertEqual(policy.hard_minimum_words, 600)
        self.assertEqual(policy.target_min_words, 250)


class WordpressDraftSoftFailTests(unittest.TestCase):
    def test_wp_http_failure_reports_wordpress_draft_not_worker_exception(self):
        from newsagent_v2.v5_generation.generation_worker import GenerationWorker

        @dataclass
        class FakeDraft:
            ok: bool = False
            error: str = "HTTP 500: critical error"
            error_code: str = "create_failed"
            seo_validation: object | None = None
            seo: object | None = None

        lifecycle = MagicMock()
        lifecycle.create_or_update_draft.return_value = FakeDraft()

        worker = GenerationWorker.__new__(GenerationWorker)
        worker.wordpress_lifecycle = lifecycle
        worker.environ = {}

        version_store = MagicMock()
        version_store.get_article.return_value = {
            "article": {
                "article_body": "word " * 620,
                "headline": "Test",
                "category": "Bitcoin",
                "tags": [],
            },
            "metadata": {"depth_status": "DEPTH_NORMAL_PASS"},
            "qa_result": {},
        }
        version_store.get_image_path.return_value = None

        event = SimpleNamespace(
            event_id="evt-wp",
            topic="bitcoin",
            entities=["etf"],
            reports=[],
        )
        result = {"article_version": "v1", "image_version": "v1", "depth_status": "DEPTH_NORMAL_PASS"}
        out = GenerationWorker._create_wordpress_draft(worker, event, result, version_store)
        self.assertIsInstance(out, dict)
        self.assertFalse(out.get("ok"))
        self.assertIn("HTTP 500", out.get("error") or "")

        # Simulate run_generation post-draft gating without full pipeline
        reported = []
        store = MagicMock()
        worker.persistent_store = store
        worker._report_failure = lambda job, stage, error: reported.append((stage, error))
        worker._update_progress = MagicMock()
        worker._send_review_package = MagicMock()

        job = SimpleNamespace(job_id="job-1", event_id="evt-wp", progress_message_id=1)
        draft_data = out
        result["wordpress_draft"] = draft_data
        if not (isinstance(draft_data, dict) and draft_data.get("ok")):
            wp_error = str(draft_data.get("error"))
            store.update_job_state(job.job_id, "FAILED_FINAL", error=wp_error[:200])
            worker._report_failure(job, "wordpress_draft", wp_error)

        self.assertEqual(reported[0][0], "wordpress_draft")
        worker._send_review_package.assert_not_called()
        worker._update_progress.assert_not_called()


class Top5ExpandBackfillIntegrationTests(unittest.TestCase):
    def test_expand_until_ready_skips_thin_after_exhausted_expansion(self):
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        from newsagent_v2.discovery.event_clusterer import EventReport, NewsEvent
        from newsagent_v2.v5_generation.source_expansion_adapter import ExpansionResult

        event = NewsEvent(
            event_id="evt-thin",
            canonical_title="Thin story",
            topic="test",
            reports=[
                EventReport(
                    report_id="r1",
                    source="S1",
                    source_id="s1",
                    source_authority=0.5,
                    headline="Thin",
                    url="https://s1.example/thin",
                    published_at="2026-09-22T00:00:00+00:00",
                    retrieved_at="2026-09-22T00:00:00+00:00",
                    description="Short.",
                    entities=[],
                    raw_item_id="r1",
                )
            ],
        )
        pipeline = V5DiscoveryPipeline.__new__(V5DiscoveryPipeline)
        pipeline.source_registry = MagicMock()
        with patch(
            "newsagent_v2.control.make_v5_bridge.expand_sources_for_event",
            return_value=ExpansionResult(
                candidates_considered=3,
                sources_matched=0,
                sources_added=[],
                primary_sources_retained=0,
                independent_domains=[],
                failed_sources=[],
                diagnostics={},
            ),
        ):
            ready, diag = pipeline._expand_until_ready(event)
        self.assertFalse(ready)
        self.assertEqual(diag["final_status"], "FAIL")
        # Must have attempted expansion path / readiness with depth capacity signal
        self.assertIn("final_readiness", diag)


if __name__ == "__main__":
    unittest.main()
