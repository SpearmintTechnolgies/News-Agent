from __future__ import annotations

import inspect
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.contract import REQUIRED_ARTICLE_FIELDS
from newsagent_v2.article.input import build_article_input
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.seo import META_MIN, SEO_TITLE_MAX
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.canonical import CANONICAL_ARTICLE_FIELDS
from newsagent_v2.article.writer.controlled.assembler import assemble_canonical_article, normalize_seo_fields
from newsagent_v2.article.writer.controlled.capacity import analyze_evidence_capacity, article_input_for_ledgers
from newsagent_v2.article.writer.controlled.config import (
    MIN_DESIRED_PUBLISHABLE_COUNT,
    ControlledWriterConfig,
    resolve_controlled_config,
)
from newsagent_v2.article.writer.controlled.failures import (
    COPYRIGHT_SIMILARITY_FAILED,
    GROUNDING_FAILED,
    INSUFFICIENT_EVIDENCE,
    QUOTE_GROUNDING_FAILED,
    SEO_ONLY_FAILED,
    WRITER_PROVIDER_ERROR,
    classify_qa_failure,
    recovery_policy,
)
from newsagent_v2.article.writer.controlled.paragraph import (
    UNAUTHORIZED_CLAIM,
    validate_paragraph,
)
from newsagent_v2.article.writer.controlled.pipeline import (
    CompileResult,
    collect_publishable_stories,
    compile_controlled_article,
)
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.controlled.renderer import (
    FakeProseRenderer,
    RenderedParagraph,
    ScriptedProseRenderer,
    controlled_renderer_messages,
)
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.batch.viability import DEFAULT_CANDIDATE_SCAN_LIMIT
from tests.fixtures.article_qa import THIN_CANDIDATE

REPO = Path(__file__).resolve().parents[1]


def _sentence(n: int) -> str:
    return (
        f"Northwind Payments disclosed review item {n} covering {12000 + n} "
        f"customer records after the spoofed email case in the retail file set."
    )


def _rich_story(event_id: str = "event-cw-001", n: int = 28) -> dict:
    text = " ".join(_sentence(i) for i in range(n))
    text += ' A Republican aide said, "This is the final offer on the table."'
    candidate = {
        "event_id": event_id,
        "representative_title": "Northwind Payments discloses spoofed-email customer-file review",
        "deterministic_rank": 1,
        "event_score": 5.0,
        "sources": ["TestWire"],
        "source_count": 1,
        "evidence": [
            {
                "source": "TestWire",
                "source_type": "newsroom",
                "source_role": "discovery",
                "source_authority": 0.8,
                "title": "Northwind Payments discloses spoofed-email customer-file review",
                "url": f"https://example.com/news/{event_id}",
                "published": "Mon, 14 Sep 2026 12:00:00 +0000",
                "summary": text,
                "extracted_text": text,
            }
        ],
    }
    article_input = build_article_input(candidate)
    article_input["category"] = "security_incident"
    return {"event_id": event_id, "article_input": article_input_for_ledgers(article_input)}


def _thin_story() -> dict:
    article_input = build_article_input(deepcopy(THIN_CANDIDATE))
    return {"event_id": article_input["event_id"], "article_input": article_input_for_ledgers(article_input)}


class ControlledWriterV3Tests(unittest.TestCase):
    def test_planner_uses_only_ledger_claim_ids(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        allowed = {row.claim_id for row in ledgers.claims}
        used = {cid for para in plan.paragraph_plans for cid in para.allowed_claim_ids}
        self.assertTrue(used)
        self.assertTrue(used <= allowed)
        self.assertTrue(set(plan.headline_claim_ids) <= allowed)

    def test_planner_cannot_create_new_claims(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        before = {row.text for row in ledgers.claims}
        plan = plan_article(ledgers, story["article_input"])
        after = {row.text for row in ledgers.claims}
        self.assertEqual(before, after)
        self.assertTrue(all(cid.startswith("C") for cid in plan.selected_claim_ids))

    def test_paragraph_validator_rejects_unsupported_assertion(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        para = plan.paragraph_plans[0]
        rendered = RenderedParagraph(
            paragraph_id=para.paragraph_id,
            text="The bill is expected to transform American crypto markets after years of neglect.",
        )
        result = validate_paragraph(rendered, para, ledgers)
        self.assertFalse(result.ok)
        self.assertTrue(any(item["code"] == "unsupported_paragraph_assertion" for item in result.issues))

    def test_paragraph_validator_rejects_unauthorized_claim(self) -> None:
        from newsagent_v2.article.writer.controlled.plan import ParagraphPlan
        from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim

        ledgers = EvidenceLedgers(
            event_id="event-cw-auth",
            claims=(
                LedgerClaim(
                    "C01",
                    "Senate Republicans released revised CLARITY Act text as a final offer to Democrats.",
                    "fact",
                    ("e1",),
                ),
                LedgerClaim(
                    "C02",
                    "Northwind Payments told TestWire that 12,400 customer records were involved.",
                    "fact",
                    ("e1",),
                ),
            ),
            quotes=(),
        )
        first = ParagraphPlan(
            paragraph_id="p01",
            editorial_purpose="lead",
            allowed_claim_ids=("C01",),
            allowed_quote_ids=(),
            target_word_range=(10, 40),
            required_claim_ids=("C01",),
            optional_claim_ids=(),
        )
        rendered = RenderedParagraph(
            paragraph_id="p01",
            text=ledgers.claim_by_id()["C02"].text,
        )
        result = validate_paragraph(rendered, first, ledgers)
        self.assertFalse(result.ok)
        self.assertTrue(any(item["code"] == UNAUTHORIZED_CLAIM for item in result.issues))

    def test_exact_approved_quote_maps(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        self.assertTrue(ledgers.quotes)
        quote = ledgers.quotes[0]
        claim = next(row for row in ledgers.claims if quote.text in row.text or True)
        from newsagent_v2.article.writer.controlled.plan import ParagraphPlan

        para = ParagraphPlan(
            paragraph_id="p99",
            editorial_purpose="attribution",
            allowed_claim_ids=(claim.claim_id,),
            allowed_quote_ids=(quote.quote_id,),
            target_word_range=(10, 40),
            required_claim_ids=(claim.claim_id,),
            optional_claim_ids=(),
        )
        rendered = RenderedParagraph(
            paragraph_id="p99",
            text=f'{claim.text} The aide said, "{quote.text}"',
        )
        result = validate_paragraph(rendered, para, ledgers)
        self.assertIn(quote.quote_id, result.mapped_quote_ids)

    def test_invented_or_modified_quote_fails(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        para = plan.paragraph_plans[0]
        claim = ledgers.claim_by_id()[para.required_claim_ids[0]]
        rendered = RenderedParagraph(
            paragraph_id=para.paragraph_id,
            text=f'{claim.text} An aide said, "This invented line does not appear in frozen evidence."',
        )
        result = validate_paragraph(rendered, para, ledgers)
        self.assertTrue(any(item["code"] == "invented_or_modified_quote" for item in result.issues))
        self.assertNotIn("This invented line does not appear in frozen evidence.", result.text)
        self.assertIn(claim.text, result.text)
        self.assertEqual(result.outcome, "PARTIALLY_RETAINED")

    def test_assembler_cannot_include_failed_paragraph(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        from newsagent_v2.article.writer.controlled.paragraph import ParagraphValidation

        good = ParagraphValidation(
            paragraph_id=plan.paragraph_plans[0].paragraph_id,
            ok=True,
            text=ledgers.claim_by_id()[plan.paragraph_plans[0].required_claim_ids[0]].text,
            mapped_claim_ids=list(plan.paragraph_plans[0].required_claim_ids),
        )
        bad = ParagraphValidation(
            paragraph_id=plan.paragraph_plans[1].paragraph_id,
            ok=False,
            text="unsupported filler about transforming markets overnight.",
            issues=[{"code": "unsupported_paragraph_assertion", "message": "no"}],
        )
        article = assemble_canonical_article(
            plan=plan,
            ledgers=ledgers,
            article_input=story["article_input"],
            validated=[good, bad],
        )
        self.assertNotIn("transforming markets", article["article_body"])
        self.assertIn(good.text[:20], article["article_body"])

    def test_canonical_article_compatibility(self) -> None:
        compiled = compile_controlled_article(_rich_story(), renderer=FakeProseRenderer())
        self.assertIsNotNone(compiled.article)
        for field in REQUIRED_ARTICLE_FIELDS:
            self.assertIn(field, compiled.article)
        for field in CANONICAL_ARTICLE_FIELDS:
            self.assertIn(field, compiled.article)
        self.assertEqual(compiled.article["schema_version"], "article-output-v1")
        self.assertTrue(compiled.ok)
        self.assertTrue(compiled.qa["qa_passed"])
        self.assertTrue(compiled.qa["publishable"])
        self.assertGreaterEqual(compiled.qa["metrics"]["article_word_count"], 350)

    def test_evidence_capacity_prevents_padding_and_keeps_floors(self) -> None:
        thin = build_evidence_ledgers(_thin_story()["article_input"])
        cap = analyze_evidence_capacity(thin)
        self.assertFalse(cap.sufficient_for_article)
        self.assertLess(cap.estimated_safe_word_max, NORMAL_ARTICLE_POLICY.hard_minimum_words)
        rich = build_evidence_ledgers(_rich_story()["article_input"])
        rich_cap = analyze_evidence_capacity(rich)
        self.assertGreaterEqual(rich_cap.estimated_safe_word_max, NORMAL_ARTICLE_POLICY.hard_minimum_words)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)

    def test_qa_and_evidence_rules_unchanged(self) -> None:
        policy_src = inspect.getsource(NORMAL_ARTICLE_POLICY.__class__)
        self.assertNotIn("hard_minimum_words=300", policy_src)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        qa_runner = (REPO / "src/newsagent_v2/article/qa/runner.py").read_text(encoding="utf-8")
        self.assertIn("check_grounding", qa_runner)
        self.assertIn("check_similarity", qa_runner)

    def test_seo_normalization_cannot_introduce_factual_claims(self) -> None:
        compiled = compile_controlled_article(_rich_story(), renderer=FakeProseRenderer())
        article = deepcopy(compiled.article)
        original_claims = deepcopy(article["claims"])
        original_body = article["article_body"]
        article["slug"] = "Not A Slug!!"
        article["seo_title"] = "x"
        article["meta_description"] = "short"
        fixed = normalize_seo_fields(article)
        self.assertEqual(fixed["claims"], original_claims)
        self.assertEqual(fixed["article_body"], original_body)
        self.assertNotEqual(fixed["slug"], "Not A Slug!!")
        self.assertLessEqual(len(fixed["seo_title"]), SEO_TITLE_MAX)
        self.assertGreaterEqual(len(fixed["meta_description"]), META_MIN)
        self.assertEqual(classify_qa_failure({"critical_failures": [{"code": "invalid_slug"}]}), SEO_ONLY_FAILED)

    def test_reserve_candidate_replaces_failed_and_stops_at_target(self) -> None:
        def compile_fn(story: dict) -> CompileResult:
            event_id = story["event_id"]
            if event_id in {"event-a", "event-c"}:
                return CompileResult(ok=False, event_id=event_id, failure_class=GROUNDING_FAILED)
            return CompileResult(
                ok=True,
                event_id=event_id,
                bundle=_dummy_bundle(event_id),
            )

        stories = [{"event_id": f"event-{letter}"} for letter in "abcdefg"]
        config = ControlledWriterConfig(target_publishable_count=3, max_candidate_attempts=10)
        result = collect_publishable_stories(stories, compile_fn=compile_fn, config=config)
        self.assertEqual(result["publishable_count"], 3)
        self.assertEqual(result["stopped_reason"], "target_reached")
        self.assertEqual(result["candidate_attempts"], 5)
        self.assertFalse(result["qa_weakened"])

    def test_search_stops_at_bounded_max_attempts(self) -> None:
        def compile_fn(story: dict) -> CompileResult:
            return CompileResult(ok=True, event_id=story["event_id"], bundle=_dummy_bundle(story["event_id"]))

        stories = [{"event_id": f"event-{i}"} for i in range(8)]
        config = ControlledWriterConfig(target_publishable_count=5, max_candidate_attempts=3)
        result = collect_publishable_stories(stories, compile_fn=compile_fn, config=config)
        self.assertEqual(result["candidate_attempts"], 3)
        self.assertEqual(result["publishable_count"], 3)
        self.assertEqual(result["stopped_reason"], "max_attempts")

    def test_minimum_desired_never_weakens_qa_and_reports_truthfully(self) -> None:
        def compile_fn(story: dict) -> CompileResult:
            if story["event_id"] == "ok-1":
                return CompileResult(ok=True, event_id="ok-1", bundle=_dummy_bundle("ok-1"))
            return CompileResult(ok=False, event_id=story["event_id"], failure_class=GROUNDING_FAILED)

        stories = [{"event_id": "ok-1"}, {"event_id": "bad-1"}, {"event_id": "bad-2"}]
        config = ControlledWriterConfig(
            target_publishable_count=5,
            min_desired_publishable_count=3,
            max_candidate_attempts=3,
        )
        result = collect_publishable_stories(stories, compile_fn=compile_fn, config=config)
        self.assertEqual(result["publishable_count"], 1)
        self.assertFalse(result["met_minimum_desired"])
        self.assertFalse(result["qa_weakened"])
        self.assertEqual(result["min_desired_publishable_count"], 3)
        self.assertEqual(len(result["failure_summary"]), 2)

    def test_provider_failure_distinct_from_qa_and_insufficient_evidence(self) -> None:
        provider = compile_controlled_article(
            _rich_story(),
            renderer=ScriptedProseRenderer(provider_error="HTTP 503"),
        )
        self.assertEqual(provider.failure_class, WRITER_PROVIDER_ERROR)
        self.assertIsNone(provider.qa)
        thin = compile_controlled_article(_thin_story(), renderer=FakeProseRenderer())
        self.assertEqual(thin.failure_class, INSUFFICIENT_EVIDENCE)
        self.assertTrue(thin.eligible_for_deeper_evidence)
        self.assertNotEqual(provider.failure_class, thin.failure_class)
        self.assertEqual(recovery_policy(INSUFFICIENT_EVIDENCE)["eligible_for_deeper_evidence"], True)
        self.assertFalse(recovery_policy(GROUNDING_FAILED)["retry_same_candidate"])
        self.assertFalse(recovery_policy(COPYRIGHT_SIMILARITY_FAILED)["retry_same_candidate"])
        self.assertTrue(recovery_policy(SEO_ONLY_FAILED)["seo_normalize"])

    def test_no_infinite_repair_retry_loop(self) -> None:
        src = (REPO / "src/newsagent_v2/article/writer/controlled/pipeline.py").read_text(encoding="utf-8")
        self.assertIn("while render_attempts < cfg.max_render_attempts", src)
        config = resolve_controlled_config()
        self.assertEqual(config.max_render_attempts, 1)
        self.assertEqual(config.max_seo_normalize_attempts, 1)
        self.assertEqual(config.make_story_count, MAKE_STORY_COUNT)
        self.assertEqual(MAKE_STORY_COUNT, 1)
        self.assertEqual(config.min_desired_publishable_count, MIN_DESIRED_PUBLISHABLE_COUNT)
        self.assertEqual(MIN_DESIRED_PUBLISHABLE_COUNT, 3)
        self.assertEqual(config.max_candidate_attempts, DEFAULT_CANDIDATE_SCAN_LIMIT)
        self.assertFalse(recovery_policy(GROUNDING_FAILED)["retry_same_candidate"])
        prompt = controlled_renderer_messages(
            plan_article(build_evidence_ledgers(_rich_story()["article_input"]), _rich_story()["article_input"]),
            build_evidence_ledgers(_rich_story()["article_input"]),
        )
        self.assertIn("PRE-APPROVED", prompt[0]["content"])
        self.assertNotIn("Write a news article about", prompt[0]["content"])

    def test_quote_ledger_integrated_in_plan(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        quote_ids = {row.quote_id for row in ledgers.quotes}
        used = {qid for para in plan.paragraph_plans for qid in para.allowed_quote_ids}
        self.assertTrue(used <= quote_ids)

    def test_failure_taxonomy_covers_required_classes(self) -> None:
        from newsagent_v2.article.writer.controlled.failures import FAILURE_CLASSES

        required = {
            "INSUFFICIENT_EVIDENCE",
            "WRITER_PROVIDER_ERROR",
            "WRITER_OUTPUT_INVALID",
            "GROUNDING_FAILED",
            "QUOTE_GROUNDING_FAILED",
            "COPYRIGHT_SIMILARITY_FAILED",
            "MECHANICS_FAILED",
            "SEO_ONLY_FAILED",
            "IMAGE_FAILED",
            "UNKNOWN_CRITICAL_FAILURE",
        }
        self.assertTrue(required <= set(FAILURE_CLASSES))
        self.assertEqual(
            classify_qa_failure({"critical_failures": [{"code": "quote_body_unmapped"}]}),
            QUOTE_GROUNDING_FAILED,
        )


def _dummy_bundle(event_id: str):
    article = {name: "x" for name in CANONICAL_ARTICLE_FIELDS}
    article.update(
        {
            "schema_version": "article-output-v1",
            "event_id": event_id,
            "entities": [],
            "keywords": [],
            "evidence_used": [],
            "claims": [],
            "quotes": [],
            "article_sections": [],
        }
    )
    return type("Bundle", (), {"article": article})()


if __name__ == "__main__":
    unittest.main()


