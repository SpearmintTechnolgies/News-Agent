"""Regression: authoritative WRITER_FEASIBLE + failed-attempt persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY, check_depth
from newsagent_v2.article.writer.controlled.pipeline import compile_controlled_article
from newsagent_v2.article.writer.controlled.renderer import FakeProseRenderer, RendererResult
from newsagent_v2.control.make_recovery import (
    persist_candidate_attempt,
    recover_publishable_article,
)
from newsagent_v2.article.writer.controlled.pipeline import CompileResult
from tests.test_writer_feasibility import _rich_researched_like_024, _thin_story


class CountingRenderer:
    renderer_name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def render(self, plan, ledgers) -> RendererResult:
        self.calls += 1
        return FakeProseRenderer().render(plan, ledgers)


class InvalidNativeRenderer:
    renderer_name = "invalid_native"

    def __init__(self) -> None:
        self.calls = 0

    def render(self, plan, ledgers) -> RendererResult:
        del ledgers
        self.calls += 1
        return RendererResult(
            ok=False,
            invalid_output=True,
            error="unauthorized_fact_relationship",
            native={
                "headline": "Test",
                "dek": "Dek",
                "paragraphs": [
                    {
                        "paragraph_id": plan.paragraph_plans[0].paragraph_id,
                        "sentences": [
                            {
                                "sentence_id": "s1",
                                "text": "Two facts glued which means markets will boom.",
                                "fact_ids_used": list(plan.selected_claim_ids[:2]),
                                "quote_ids_used": [],
                            }
                        ],
                    }
                ],
            },
        )


class AuthoritativeFeasibilityTests(unittest.TestCase):
    def test_writer_infeasible_does_not_call_writer(self) -> None:
        renderer = CountingRenderer()
        compiled = compile_controlled_article(_thin_story(), renderer=renderer)
        self.assertFalse(compiled.ok)
        self.assertEqual(renderer.calls, 0)
        self.assertEqual(compiled.failure_class, "WRITER_INFEASIBLE")

    def test_admitted_writer_feasible_ignores_legacy_capacity_veto(self) -> None:
        story = _rich_researched_like_024()
        story["_writer_feasible"] = True
        renderer = CountingRenderer()
        compiled = compile_controlled_article(story, renderer=renderer)
        self.assertGreaterEqual(renderer.calls, 1)
        self.assertNotEqual(compiled.failure_class, "INSUFFICIENT_EVIDENCE")
        self.assertNotIn("cannot reasonably support the hard article minimum", compiled.notes)
        self.assertTrue(compiled.writer_feasible)
        self.assertEqual(compiled.capacity_advisory.get("capacity_class"), "INSUFFICIENT_EVIDENCE")

    def test_final_qa_still_fails_below_350(self) -> None:
        article = {"article_body": " ".join(["custody"] * 347)}
        issues, _ = check_depth(article, _rich_researched_like_024()["article_input"], NORMAL_ARTICLE_POLICY)
        self.assertIn("below_article_minimum_length", [i.get("code") for i in issues])
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_failed_qa_candidate_persists_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "attempts"
            compiled = CompileResult(
                ok=False,
                event_id="event-005",
                failure_class="MECHANICS_FAILED",
                article={
                    "headline": "Headline",
                    "dek": "Dek.",
                    "article_body": "body " * 400,
                },
                qa={
                    "qa_passed": False,
                    "publishable": False,
                    "critical_failures": [{"code": "malformed_punctuation", "message": "bad"}],
                    "metrics": {
                        "article_word_count": 400,
                        "body_claim_coverage": 1.0,
                        "exact_overlap_count": 0,
                    },
                },
                notes="failed",
                writer_feasible=True,
                capacity_advisory={"capacity_class": "INSUFFICIENT_EVIDENCE"},
            )
            dest = persist_candidate_attempt(
                attempts_root=root,
                event_id="event-005",
                rank=2,
                compiled=compiled,
                writer_provider="bedrock_mantle",
                writer_model="moonshotai.kimi-k2.5",
                primary_calls=1,
                supplemental_calls=1,
            )
            self.assertTrue((dest / "article.json").is_file())
            self.assertTrue((dest / "article_body.txt").is_file())
            self.assertTrue((dest / "qa.json").is_file())
            attempt = json.loads((dest / "attempt.json").read_text(encoding="utf-8"))
            self.assertEqual(attempt["critical_failure_codes"], ["malformed_punctuation"])
            self.assertEqual(attempt["failure_class"], "MECHANICS_FAILED")
            self.assertEqual(attempt["writer_provider"], "bedrock_mantle")

    def test_writer_output_invalid_persists_sanitized_diagnostic(self) -> None:
        story = _rich_researched_like_024()
        story["_writer_feasible"] = True
        renderer = InvalidNativeRenderer()
        compiled = compile_controlled_article(story, renderer=renderer)
        self.assertEqual(compiled.failure_class, "WRITER_OUTPUT_INVALID")
        self.assertIn("native", compiled.diagnostic)
        with tempfile.TemporaryDirectory() as tmp:
            dest = persist_candidate_attempt(
                attempts_root=Path(tmp) / "attempts",
                event_id=compiled.event_id,
                rank=1,
                compiled=compiled,
                writer_provider="bedrock_mantle",
                writer_model="moonshotai.kimi-k2.5",
                primary_calls=1,
                supplemental_calls=0,
            )
            blob = (dest / "writer_diagnostic.json").read_text(encoding="utf-8")
            self.assertIn("unauthorized_fact_relationship", blob)
            self.assertIn("fact_ids_used", blob)

    def test_persisted_diagnostics_have_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            compiled = CompileResult(
                ok=False,
                event_id="e1",
                failure_class="WRITER_OUTPUT_INVALID",
                diagnostic={
                    "error": "bad",
                    "api_key": "SECRET-SHOULD-NOT-LEAK",
                    "headers": {"Authorization": "Bearer SECRET-SHOULD-NOT-LEAK"},
                },
                article={"api_key": "SECRET-SHOULD-NOT-LEAK", "article_body": "ok"},
                qa={"token": "SECRET-SHOULD-NOT-LEAK", "critical_failures": []},
            )
            dest = persist_candidate_attempt(
                attempts_root=Path(tmp) / "attempts",
                event_id="e1",
                rank=1,
                compiled=compiled,
                writer_provider="bedrock_mantle",
                writer_model="moonshotai.kimi-k2.5",
                primary_calls=1,
                supplemental_calls=0,
            )
            for name in ("article.json", "qa.json", "writer_diagnostic.json"):
                text = (dest / name).read_text(encoding="utf-8")
                self.assertNotIn("SECRET-SHOULD-NOT-LEAK", text)
                self.assertIn("[REDACTED]", text)
            attempt_text = (dest / "attempt.json").read_text(encoding="utf-8")
            self.assertNotIn("SECRET-SHOULD-NOT-LEAK", attempt_text)

    def test_recover_sets_authoritative_admission_flag(self) -> None:
        calls: list[bool] = []

        def compile_fn(story: dict) -> CompileResult:
            calls.append(bool(story.get("_writer_feasible")))
            return CompileResult(
                ok=True,
                event_id=str(story["event_id"]),
                article={"headline": "H", "dek": "D", "article_body": "body " * 400},
                qa={
                    "qa_passed": True,
                    "publishable": True,
                    "metrics": {"article_word_count": 400, "body_claim_coverage": 1.0},
                },
            )

        def assess(_story: dict) -> dict:
            return {
                "eligible": True,
                "writer_feasible": True,
                "feasibility_status": "WRITER_FEASIBLE",
                "planned_safe_words": 319,
                "capacity_class": "INSUFFICIENT_EVIDENCE",
            }

        result = recover_publishable_article(
            [{"event_id": "event-024", "original_rank": 1, "article_input": {}}],
            compile_fn=compile_fn,
            assess_fn=assess,
            research_fn=lambda s: s,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()


