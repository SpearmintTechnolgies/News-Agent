from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY
from newsagent_v2.article.qa.grounding import check_grounding
from newsagent_v2.article.writer.controlled.capacity import (
    CLASS_BORDERLINE,
    CLASS_INSUFFICIENT,
    CLASS_SUFFICIENT,
    analyze_evidence_capacity,
    article_input_for_ledgers,
    refine_capacity_with_plan,
)
from newsagent_v2.article.writer.controlled.config import CAPACITY_SAFETY_MARGIN_WORDS
from newsagent_v2.article.writer.controlled.overlap_audit import (
    CLASS_CLAIM_LEAK,
    CLASS_HEADLINE_DEK,
    CLASS_SUBHEADING,
    audit_persisted_v3_run,
)
from newsagent_v2.article.writer.controlled.pipeline import (
    CompileResult,
    collect_publishable_stories,
    story_capacity_class,
)
from newsagent_v2.article.writer.controlled.plan import (
    MAX_SUBHEADING_WORDS,
    is_source_fragment,
    plan_article,
)
from newsagent_v2.article.writer.controlled.renderer import controlled_renderer_payload
from newsagent_v2.article.writer.controlled.semantic import (
    semantic_adds_no_facts,
    semantic_fact_from_claim,
)
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from tests.test_controlled_writer_v3 import _dummy_bundle, _rich_story, _thin_story

REPO = Path(__file__).resolve().parents[1]
V3_RUN = (
    REPO
    / "benchmarks"
    / "writer_bakeoff"
    / EVENT_ID
    / "live_runs"
    / "20260916T061452Z"
    / "groq_gpt_oss_20b_controlled_writer_v3"
)


class ControlledWriterV31Tests(unittest.TestCase):
    def test_semantic_preserves_meaning_and_cannot_add_facts(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        ledgers = build_evidence_ledgers(article_input_for_ledgers(fixture["article_input"]))
        claim = ledgers.claims[0]
        fact = semantic_fact_from_claim(claim)
        self.assertTrue(fact.subject)
        self.assertTrue(fact.relation or fact.object)
        self.assertTrue(semantic_adds_no_facts(fact, claim))
        payload = json.dumps(fact.as_dict())
        self.assertNotEqual(payload.strip(), claim.text.strip())
        self.assertLess(len(fact.object.split()), len(claim.text.split()))

    def test_original_evidence_remains_for_qa(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        original = [row.text for row in ledgers.claims]
        for claim in ledgers.claims:
            semantic_fact_from_claim(claim)
        self.assertEqual([row.text for row in ledgers.claims], original)
        self.assertTrue(any("CLARITY Act" in text for text in original))

    def test_renderer_payload_omits_long_source_sentences(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        plan = plan_article(ledgers, pack)
        payload = controlled_renderer_payload(plan, ledgers)
        blob = json.dumps(payload)
        self.assertIn("proposition_frames", blob)
        self.assertNotIn("allowed_semantic_facts", blob)
        self.assertNotIn("preferred wording", json.dumps(payload.get("paragraph_plans")))
        for claim in ledgers.claims:
            if len(claim.text.split()) >= 20:
                self.assertNotIn(claim.text, blob)
        allowed_quotes = {
            qid
            for para in plan.paragraph_plans
            for qid in para.allowed_quote_ids
        }
        quotes = {row.quote_id: row.text for row in ledgers.quotes}
        self.assertTrue(quotes)
        for qid, text in quotes.items():
            if qid in allowed_quotes:
                self.assertIn(text, blob)

    def test_legal_names_remain_intact(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        ledgers = build_evidence_ledgers(article_input_for_ledgers(fixture["article_input"]))
        blob = " ".join(
            json.dumps(semantic_fact_from_claim(claim).as_dict()) for claim in ledgers.claims
        )
        self.assertIn("CLARITY Act", blob)
        self.assertTrue(any("Lummis" in json.dumps(semantic_fact_from_claim(c).as_dict()) for c in ledgers.claims))

    def test_headline_dek_subheadings_are_editorial_not_source(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        plan = plan_article(ledgers, pack)
        titles = [str(row.get("title") or "") for row in pack.get("evidence") or []]
        req = " ".join(plan.headline_requirements.values())
        dek = " ".join(plan.dek_requirements.values())
        for title in titles:
            self.assertFalse(is_source_fragment(req, claim_texts=[], titles=[title]) and req == title)
            self.assertNotEqual(req.strip().lower(), title.strip().lower())
            self.assertNotEqual(dek.strip().lower(), title.strip().lower())
        self.assertTrue(plan.headline_requirements.get("primary_actor"))
        self.assertTrue(plan.headline_requirements.get("primary_action"))
        for sub in plan.subheading_plans:
            self.assertLessEqual(len(sub.split()), MAX_SUBHEADING_WORDS)
            self.assertFalse(
                is_source_fragment(
                    sub,
                    claim_texts=[row.text for row in ledgers.claims],
                    titles=titles,
                )
            )
            self.assertFalse(sub.startswith("President Trump voluntarily"))
            self.assertFalse(sub.startswith("The revised ethics rules would allow"))
        self.assertIn("CLARITY Act", " ".join(row.text for row in ledgers.claims))

    def test_capacity_margin_and_event005_borderline(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertGreaterEqual(CAPACITY_SAFETY_MARGIN_WORDS, 40)
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        ledgers = build_evidence_ledgers(article_input_for_ledgers(fixture["article_input"]))
        cap = analyze_evidence_capacity(ledgers)
        self.assertEqual(cap.capacity_class, CLASS_BORDERLINE)
        self.assertTrue(cap.sufficient_for_article)
        self.assertGreaterEqual(cap.min_planned_safe_words, 350 + CAPACITY_SAFETY_MARGIN_WORDS)
        thin = analyze_evidence_capacity(build_evidence_ledgers(_thin_story()["article_input"]))
        self.assertEqual(thin.capacity_class, CLASS_INSUFFICIENT)
        rich = analyze_evidence_capacity(build_evidence_ledgers(_rich_story()["article_input"]))
        self.assertEqual(rich.capacity_class, CLASS_SUFFICIENT)

    def test_paragraph_loss_risk_before_generation(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        plan = plan_article(ledgers, pack)
        self.assertGreater(plan.planned_safe_words, 0)
        self.assertIsInstance(plan.paragraph_loss_tolerance, int)
        self.assertGreaterEqual(plan.minimum_surviving_words, 0)
        cap = refine_capacity_with_plan(
            analyze_evidence_capacity(ledgers),
            planned_safe_words=plan.planned_safe_words,
            minimum_surviving_words=plan.minimum_surviving_words,
            paragraph_loss_tolerance=plan.paragraph_loss_tolerance,
        )
        self.assertEqual(cap.capacity_class, CLASS_BORDERLINE)
        self.assertEqual(cap.paragraph_loss_tolerance, plan.paragraph_loss_tolerance)

    def test_reserve_prefers_sufficient_over_borderline(self) -> None:
        def compile_fn(story: dict) -> CompileResult:
            return CompileResult(
                ok=True,
                event_id=story["event_id"],
                bundle=_dummy_bundle(story["event_id"]),
            )

        stories = [
            {"event_id": "borderline-first", "capacity_class": CLASS_BORDERLINE},
            {"event_id": "sufficient-second", "capacity_class": CLASS_SUFFICIENT},
        ]
        from newsagent_v2.article.writer.controlled.config import ControlledWriterConfig

        result = collect_publishable_stories(
            stories,
            compile_fn=compile_fn,
            config=ControlledWriterConfig(target_publishable_count=1, max_candidate_attempts=1),
        )
        self.assertEqual(result["publishable_count"], 1)
        self.assertEqual(result["attempts"][0].event_id, "sufficient-second")
        self.assertEqual(story_capacity_class(stories[0]), CLASS_BORDERLINE)
        self.assertEqual(story_capacity_class(stories[1]), CLASS_SUFFICIENT)

    def test_qa_grounding_similarity_floors_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        grounding_src = (REPO / "src/newsagent_v2/article/qa/grounding.py").read_text(encoding="utf-8")
        similarity_src = (REPO / "src/newsagent_v2/article/qa/similarity.py").read_text(encoding="utf-8")
        self.assertIn("body_assertion_not_in_claims", grounding_src)
        self.assertIn("exact_phrase_overlap", similarity_src)
        self.assertIn("HIGH_SENTENCE_SIMILARITY = 0.92", similarity_src)

    def test_offline_overlap_audit_of_persisted_v3(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        report = audit_persisted_v3_run(V3_RUN, article_input=fixture["article_input"])
        self.assertEqual(report["qa_max_similarity"], 1.0)
        self.assertEqual(report["qa_exact_overlap_count"], 5)
        self.assertEqual(report["qa_ngram_hits"], 255)
        self.assertTrue(report["source_language_leakage"])
        self.assertEqual(report["headline_class"], CLASS_HEADLINE_DEK)
        self.assertIn(CLASS_CLAIM_LEAK, report["high_similarity_class_counts"])
        self.assertTrue(any("voluntarily agreed" in item for item in report["planned_subheadings"]))
        pair = report["max_similarity_pair"] or {}
        self.assertGreaterEqual(pair.get("ratio") or 0, 0.99)


if __name__ == "__main__":
    unittest.main()


