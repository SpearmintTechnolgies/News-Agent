from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY
from newsagent_v2.article.writer.controlled.assembler import assemble_canonical_article
from newsagent_v2.article.writer.controlled.capacity import (
    CLASS_BORDERLINE,
    analyze_evidence_capacity,
    article_input_for_ledgers,
)
from newsagent_v2.article.writer.controlled.config import CAPACITY_SAFETY_MARGIN_WORDS
from newsagent_v2.article.writer.controlled.paragraph import (
    AMBIGUOUS_ASSERTION,
    DEPENDENCY_INVALID,
    INSEPARABLE_UNSUPPORTED,
    OUTCOME_FULLY_REJECTED,
    OUTCOME_PARTIALLY_RETAINED,
    UNAUTHORIZED_CLAIM,
    UNAUTHORIZED_QUOTE,
    UNSUPPORTED_ASSERTION,
    ParagraphValidation,
    validate_paragraph,
)
from newsagent_v2.article.writer.controlled.plan import ParagraphPlan, plan_article
from newsagent_v2.article.writer.controlled.quarantine import replay_persisted_v3_generation
from newsagent_v2.article.writer.controlled.renderer import RenderedParagraph, controlled_renderer_payload
from newsagent_v2.article.writer.controlled.semantic import SemanticFact, semantic_fact_from_claim
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote, build_evidence_ledgers
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from tests.test_controlled_writer_v3 import _rich_story, _sentence

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


UNSUPPORTED = "The bill is expected to transform American crypto markets after years of neglect."
UNAUTHORIZED = (
    "Senate Republicans released revised CLARITY Act text as a final offer to Democrats."
)
AMBIGUOUS = (
    "Northwind Payments disclosed something after lunch with Boston analysts today."
)


def _ledgers() -> EvidenceLedgers:
    claims = tuple(LedgerClaim(f"C{n:02d}", _sentence(n), "fact", ("e1",)) for n in range(1, 5)) + (
        LedgerClaim("C99", UNAUTHORIZED, "fact", ("e1",)),
    )
    quotes = (
        LedgerQuote("Q01", "This is the final offer on the table.", "a Republican aide", "e1"),
        LedgerQuote("Q02", "The records were copied overnight.", "a spokesperson", "e1"),
    )
    return EvidenceLedgers(event_id="event-cw-q", claims=claims, quotes=quotes)


def _plan(claim_ids: tuple[str, ...], quote_ids: tuple[str, ...] = ()) -> ParagraphPlan:
    return ParagraphPlan(
        paragraph_id="p01",
        editorial_purpose="lead",
        allowed_claim_ids=claim_ids,
        allowed_quote_ids=quote_ids,
        target_word_range=(10, 80),
        required_claim_ids=claim_ids,
        optional_claim_ids=(),
    )


class ControlledWriterV32Tests(unittest.TestCase):
    def test_five_sentence_paragraph_keeps_four_grounded(self) -> None:
        ledgers = _ledgers()
        plan = _plan(("C01", "C02", "C03", "C04"))
        sentences = [_sentence(n) for n in range(1, 5)] + [UNSUPPORTED]
        rendered = RenderedParagraph(paragraph_id="p01", text=" ".join(sentences))
        result = validate_paragraph(rendered, plan, ledgers)
        self.assertEqual(result.outcome, OUTCOME_PARTIALLY_RETAINED)
        for n in range(1, 5):
            self.assertIn(_sentence(n), result.text)
        self.assertNotIn(UNSUPPORTED, result.text)
        self.assertEqual(result.text, " ".join(_sentence(n) for n in range(1, 5)))
        self.assertTrue(any(item["code"] == UNSUPPORTED_ASSERTION for item in result.issues))

    def test_unsupported_sentence_absent_from_assembled_article(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        para = plan.paragraph_plans[0]
        good = ledgers.claim_by_id()[para.required_claim_ids[0]].text
        rendered = RenderedParagraph(paragraph_id=para.paragraph_id, text=f"{good} {UNSUPPORTED}")
        validated = validate_paragraph(rendered, para, ledgers)
        article = assemble_canonical_article(
            plan=plan,
            ledgers=ledgers,
            article_input=story["article_input"],
            validated=[validated],
        )
        self.assertNotIn("transform American crypto markets", article["article_body"])
        self.assertIn(good, article["article_body"])

    def test_ambiguous_sentence_is_quarantined(self) -> None:
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=f"{_sentence(1)} {AMBIGUOUS}"),
            _plan(("C01", "C02", "C03", "C04")),
            _ledgers(),
        )
        self.assertNotIn("conference call", result.text)
        self.assertTrue(any(item["code"] == AMBIGUOUS_ASSERTION for item in result.issues))

    def test_unauthorized_cross_paragraph_claim_quarantined(self) -> None:
        ledgers = _ledgers()
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=f"{_sentence(1)} {UNAUTHORIZED}"),
            _plan(("C01",)),
            ledgers,
        )
        self.assertIn(_sentence(1), result.text)
        self.assertNotIn(UNAUTHORIZED, result.text)
        self.assertTrue(any(item["code"] == UNAUTHORIZED_CLAIM for item in result.issues))

    def test_unauthorized_quote_is_quarantined(self) -> None:
        ledgers = _ledgers()
        quote = ledgers.quotes[0].text
        text = f'{_sentence(1)} A Republican aide said, "{quote}"'
        result = validate_paragraph(RenderedParagraph(paragraph_id="p01", text=text), _plan(("C01",)), ledgers)
        self.assertIn(_sentence(1), result.text)
        self.assertNotIn(quote, result.text)
        self.assertTrue(any(item["code"] == UNAUTHORIZED_QUOTE for item in result.issues))

    def test_inseparable_multi_claim_sentence_quarantined_entirely(self) -> None:
        ledgers = _ledgers()
        grounded = _sentence(1).rstrip(".")
        mixed = f"{grounded}, but the sector will be transformed overnight after years of neglect."
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=mixed),
            _plan(("C01", "C02", "C03", "C04")),
            ledgers,
        )
        self.assertEqual(result.outcome, OUTCOME_FULLY_REJECTED)
        self.assertEqual(result.text, "")
        self.assertTrue(any(item["code"] == INSEPARABLE_UNSUPPORTED for item in result.issues))
        self.assertNotIn(grounded, result.text)

    def test_separable_sentences_validated_independently(self) -> None:
        ledgers = _ledgers()
        text = f"{_sentence(1)} {UNSUPPORTED} {_sentence(2)}"
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=text),
            _plan(("C01", "C02", "C03", "C04")),
            ledgers,
        )
        self.assertIn(_sentence(1), result.text)
        self.assertIn(_sentence(2), result.text)
        self.assertNotIn(UNSUPPORTED, result.text)

    def test_dependent_sentence_quarantined_without_antecedent(self) -> None:
        ledgers = _ledgers()
        dependent = (
            "These restrictions covering 12001 customer records after the spoofed email "
            "case in the retail file set were disclosed by Northwind Payments."
        )
        independent = _sentence(2)
        text = f"{UNSUPPORTED} {dependent} {independent}"
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=text),
            _plan(("C01", "C02", "C03", "C04")),
            ledgers,
        )
        self.assertTrue(any(item["code"] == DEPENDENCY_INVALID for item in result.issues))
        self.assertNotIn("These restrictions", result.text)
        self.assertIn(independent, result.text)
        self.assertNotIn(UNSUPPORTED, result.text)

    def test_retained_prose_is_unchanged_and_no_repair(self) -> None:
        ledgers = _ledgers()
        keep = _sentence(3)
        generated = f"{_sentence(1)} {UNSUPPORTED} {keep}"
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=generated),
            _plan(("C01", "C02", "C03", "C04")),
            ledgers,
        )
        self.assertEqual(result.generated_text, generated)
        self.assertIn(keep, result.text)
        unit = next(row for row in result.units if row.text == keep)
        self.assertEqual(unit.text, keep)
        self.assertNotIn("However", result.text)
        self.assertNotIn("transform American", result.text)

    def test_partial_retained_enters_assembler_rejected_does_not(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        keep = ledgers.claim_by_id()[plan.paragraph_plans[0].required_claim_ids[0]].text
        partial = validate_paragraph(
            RenderedParagraph(paragraph_id=plan.paragraph_plans[0].paragraph_id, text=f"{keep} {UNSUPPORTED}"),
            plan.paragraph_plans[0],
            ledgers,
        )
        rejected = ParagraphValidation(
            paragraph_id=plan.paragraph_plans[1].paragraph_id,
            ok=False,
            text="quarantined filler about transforming markets overnight.",
            outcome=OUTCOME_FULLY_REJECTED,
            generated_text="quarantined filler about transforming markets overnight.",
        )
        article = assemble_canonical_article(
            plan=plan,
            ledgers=ledgers,
            article_input=story["article_input"],
            validated=[partial, rejected],
        )
        self.assertEqual(partial.outcome, OUTCOME_PARTIALLY_RETAINED)
        self.assertIn(keep, article["article_body"])
        self.assertNotIn("transforming markets", article["article_body"])
        self.assertNotIn(UNSUPPORTED, article["article_body"])

    def test_v31_invariants_and_qa_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(CAPACITY_SAFETY_MARGIN_WORDS, 80)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        self.assertEqual(CAPACITY_SAFETY_MARGIN_WORDS, 80)
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        cap = analyze_evidence_capacity(ledgers)
        self.assertEqual(cap.capacity_class, CLASS_BORDERLINE)
        fact = semantic_fact_from_claim(ledgers.claims[0])
        self.assertIsInstance(fact, SemanticFact)
        payload = controlled_renderer_payload(plan_article(ledgers, pack), ledgers)
        self.assertIn("proposition_frames", payload["paragraph_plans"][0])
        self.assertNotIn("allowed_semantic_facts", payload["paragraph_plans"][0])
        self.assertNotIn("allowed_claims", payload["paragraph_plans"][0])
        grounding_src = (REPO / "src/newsagent_v2/article/qa/grounding.py").read_text(encoding="utf-8")
        similarity_src = (REPO / "src/newsagent_v2/article/qa/similarity.py").read_text(encoding="utf-8")
        self.assertIn("body_assertion_not_in_claims", grounding_src)
        self.assertIn("exact_phrase_overlap", similarity_src)
        self.assertNotIn("quarantine", inspect.getsource(NORMAL_ARTICLE_POLICY.__class__))

    def test_offline_replay_of_persisted_v3_p01(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        report = replay_persisted_v3_generation(V3_RUN, ledgers=ledgers, article_input=pack)
        p01 = report["p01"]
        self.assertGreater(p01["original_words"], 0)
        self.assertIn(p01["outcome"], {OUTCOME_PARTIALLY_RETAINED, OUTCOME_FULLY_REJECTED})
        self.assertEqual(report["old_assembled_word_count"], 265)
        self.assertFalse(report["historical_artifacts_modified"])
        self.assertIn("not an autonomous winner", report["label"])
        native = (V3_RUN / "native.json").read_text(encoding="utf-8")
        self.assertIn("is aimed at swaying lawmakers", native)


if __name__ == "__main__":
    unittest.main()


