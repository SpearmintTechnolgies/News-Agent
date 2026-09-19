from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.controlled.groq_oss20 import parse_controlled_v3_native
from newsagent_v2.article.writer.controlled.paragraph import OUTCOME_PARTIALLY_RETAINED, validate_paragraph
from newsagent_v2.article.writer.controlled.plan import ParagraphPlan, plan_article
from newsagent_v2.article.writer.controlled.renderer import controlled_renderer_payload
from newsagent_v2.article.writer.controlled.renderer_contract import (
    CROSS_PARAGRAPH_FACT,
    UNAUTHORIZED_RELATIONSHIP,
    UNKNOWN_FACT_ID,
    editorial_truncation_issues,
    paragraph_fact_budget,
    validate_sentence_declarations,
)
from newsagent_v2.article.writer.controlled.semantic import (
    distinctive_source_syntax,
    semantic_adds_no_facts,
    semantic_fact_from_claim,
)
from newsagent_v2.article.writer.controlled.v33_forensics import analyze_v32_run
from newsagent_v2.article.writer.evidence_ledger import LedgerClaim, build_evidence_ledgers
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from tests.test_controlled_writer_v3 import _rich_story
from tests.test_controlled_writer_v32 import _ledgers, _plan, _sentence

REPO = Path(__file__).resolve().parents[1]
V32_RUN = (
    REPO
    / "benchmarks"
    / "writer_bakeoff"
    / "controlled_v32_fresh"
    / "live_runs"
    / "20260916T075412Z"
    / "groq_gpt_oss_20b_controlled_writer_v32"
)


def _assert_groq_required_covers_properties(node: dict, path: str = "$") -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" and isinstance(node.get("properties"), dict):
        props = set(node["properties"])
        required = node.get("required")
        assert isinstance(required, list), f"{path} missing required"
        assert set(required) == props, f"{path} required {required} != properties {sorted(props)}"
        for name, child in node["properties"].items():
            _assert_groq_required_covers_properties(child, f"{path}.{name}")
    if node.get("type") == "array" and isinstance(node.get("items"), dict):
        _assert_groq_required_covers_properties(node["items"], f"{path}[]")


class ControlledWriterV33Tests(unittest.TestCase):
    def test_groq_schema_lists_every_property_in_required(self) -> None:
        from newsagent_v2.article.writer.schema import groq_controlled_v3_json_schema

        schema = groq_controlled_v3_json_schema()
        _assert_groq_required_covers_properties(schema)
        paragraph_props = schema["properties"]["paragraphs"]["items"]["properties"]
        self.assertNotIn("text", paragraph_props)
        self.assertIn("sentences", paragraph_props)

    def test_payload_requires_fact_budget_and_forbids_padding(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        payload = controlled_renderer_payload(plan, ledgers)
        blob = json.dumps(payload)
        self.assertIn("fact_ids_used", blob)
        self.assertIn("required_fact_ids", blob)
        self.assertFalse(payload["requirements"]["pad_to_word_target"])
        self.assertTrue(payload["requirements"]["one_sentence_one_assertion"])
        self.assertIn("could push", " ".join(payload["requirements"]["forbidden_implication_phrases"]))
        para = payload["paragraph_plans"][0]
        self.assertEqual(para["max_factual_assertions"], paragraph_fact_budget(plan.paragraph_plans[0]))
        self.assertNotIn("text", para["proposition_frames"][0])
        self.assertNotIn("allowed_semantic_facts", para)
        for claim in ledgers.claims:
            if len(claim.text.split()) >= 20:
                self.assertNotIn(claim.text, blob)

    def test_declaration_rejects_unknown_and_cross_paragraph_facts(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        p1 = plan.paragraph_plans[0]
        p2 = plan.paragraph_plans[1]
        native = {
            "paragraphs": [
                {
                    "paragraph_id": p1.paragraph_id,
                    "sentences": [
                        {
                            "sentence_id": "s1",
                            "text": _sentence(1),
                            "fact_ids_used": ["C999"],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues = validate_sentence_declarations(native, plan)
        self.assertTrue(any(item["code"] == UNKNOWN_FACT_ID for item in issues))
        native2 = {
            "paragraphs": [
                {
                    "paragraph_id": p1.paragraph_id,
                    "sentences": [
                        {
                            "sentence_id": "s1",
                            "text": "Northwind disclosed records which means markets will boom.",
                            "fact_ids_used": [p1.allowed_claim_ids[0], p2.allowed_claim_ids[0]],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues2 = validate_sentence_declarations(native2, plan)
        self.assertFalse(any(item["code"] == CROSS_PARAGRAPH_FACT for item in issues2))
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues2))
        parsed = parse_controlled_v3_native(native2, plan)
        self.assertTrue(parsed.invalid_output)

    def test_article_authorized_fact_may_appear_in_other_paragraph(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        p1 = plan.paragraph_plans[0]
        p2 = plan.paragraph_plans[1]
        foreign = p2.allowed_claim_ids[0]
        native = {
            "paragraphs": [
                {
                    "paragraph_id": p1.paragraph_id,
                    "sentences": [
                        {
                            "sentence_id": "s1",
                            "text": "Northwind Payments disclosed the spoofed-email review.",
                            "fact_ids_used": [foreign],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues = validate_sentence_declarations(native, plan)
        self.assertFalse(any(item["code"] == CROSS_PARAGRAPH_FACT for item in issues))
        self.assertFalse(any(item["code"] == UNKNOWN_FACT_ID for item in issues))
        parsed = parse_controlled_v3_native(native, plan)
        self.assertTrue(parsed.ok)

    def test_one_fact_and_authorized_multi_fact_declarations(self) -> None:
        plan = _plan(("C01", "C02"))
        plan = ParagraphPlan(
            paragraph_id=plan.paragraph_id,
            editorial_purpose=plan.editorial_purpose,
            allowed_claim_ids=plan.allowed_claim_ids,
            allowed_quote_ids=plan.allowed_quote_ids,
            target_word_range=plan.target_word_range,
            required_claim_ids=plan.required_claim_ids,
            optional_claim_ids=plan.optional_claim_ids,
            relationship="SEQUENCE",
        )
        from newsagent_v2.article.writer.controlled.plan import ArticlePlan

        article_plan = ArticlePlan(
            event_id="e",
            headline_claim_ids=("C01",),
            dek_claim_ids=("C02",),
            paragraph_plans=(plan,),
            subheading_plans=(),
            seo_inputs={},
            category="other",
            selected_claim_ids=("C01", "C02"),
            selected_quote_ids=(),
            headline_requirements={},
            dek_requirements={},
            planned_safe_words=40,
            minimum_surviving_words=40,
            paragraph_loss_tolerance=0,
        )
        native = {
            "paragraphs": [
                {
                    "paragraph_id": "p01",
                    "sentences": [
                        {
                            "sentence_id": "s1",
                            "text": _sentence(1),
                            "fact_ids_used": ["C01"],
                            "quote_ids_used": [],
                        },
                        {
                            "sentence_id": "s2",
                            "text": f"{_sentence(1)} {_sentence(2)}",
                            "fact_ids_used": ["C01", "C02"],
                            "quote_ids_used": [],
                        },
                    ],
                }
            ]
        }
        self.assertEqual(validate_sentence_declarations(native, article_plan), [])

    def test_quotes_remain_explicitly_mapped(self) -> None:
        from newsagent_v2.article.writer.controlled.plan import ArticlePlan

        ledgers = _ledgers()
        plan = _plan(("C01",), quote_ids=("Q01",))
        payload = controlled_renderer_payload(
            ArticlePlan(
                event_id="event-cw-q",
                headline_claim_ids=("C01",),
                dek_claim_ids=(),
                paragraph_plans=(plan,),
                subheading_plans=(),
                seo_inputs={},
                category="other",
                selected_claim_ids=("C01",),
                selected_quote_ids=("Q01",),
                headline_requirements={},
                dek_requirements={},
                planned_safe_words=20,
                minimum_surviving_words=20,
                paragraph_loss_tolerance=0,
            ),
            ledgers,
        )
        quotes = payload["paragraph_plans"][0]["allowed_quotes"]
        self.assertEqual(quotes[0]["quote_id"], "Q01")
        self.assertEqual(quotes[0]["exact_text"], ledgers.quotes[0].text)

    def test_semantic_fact_delexicalization_drops_source_spans(self) -> None:
        claim = LedgerClaim(
            "C02",
            "The prolonged uncertainty could push investment and development toward jurisdictions such as the European Union, where the Markets in Crypto-Assets (MiCA) regulation provides a clearer rulebook.",
            "fact",
            ("e1",),
        )
        fact = semantic_fact_from_claim(claim)
        self.assertTrue(semantic_adds_no_facts(fact, claim))
        self.assertFalse(distinctive_source_syntax(fact.subject, claim.text))
        self.assertFalse(distinctive_source_syntax(fact.object, claim.text))
        self.assertLess(len(fact.object.split()), 8)
        self.assertIn("European", " ".join([fact.subject, fact.object, " ".join(fact.proper_names)]))

    def test_original_evidence_still_available(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        ledgers = build_evidence_ledgers(article_input_for_ledgers(fixture["article_input"]))
        original = [row.text for row in ledgers.claims]
        for claim in ledgers.claims:
            semantic_fact_from_claim(claim)
        self.assertEqual([row.text for row in ledgers.claims], original)

    def test_quarantine_and_qa_invariants(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        self.assertEqual(OUTCOME_PARTIALLY_RETAINED, "PARTIALLY_RETAINED")
        ledgers = _ledgers()
        plan = _plan(("C01", "C02", "C03", "C04"))
        from newsagent_v2.article.writer.controlled.renderer import RenderedParagraph

        result = validate_paragraph(
            RenderedParagraph(
                paragraph_id="p01",
                text=f"{_sentence(1)} The bill is expected to transform American crypto markets after years of neglect.",
            ),
            plan,
            ledgers,
        )
        self.assertEqual(result.outcome, OUTCOME_PARTIALLY_RETAINED)

    def test_editorial_truncation_detector_not_qa(self) -> None:
        issues = editorial_truncation_issues(
            "The prolonged uncertainty could push investment toward jurisdictions such as the European",
            field="dek",
        )
        self.assertTrue(any(item["code"] == "truncated_terminal_phrase" for item in issues))
        headline_issues = editorial_truncation_issues(
            "Industry Executives Said SEC CFTC Over Asset Managers and Crypto Companies",
            field="headline",
        )
        self.assertTrue(any(item["code"] == "malformed_headline_relation" for item in headline_issues))

    def test_offline_v32_forensics_counts(self) -> None:
        report = analyze_v32_run(V32_RUN)
        self.assertEqual(report["total_assertions"], 34)
        self.assertEqual(report["supported"], 16)
        quarantined = report["quarantined_rows"]
        self.assertEqual(len(quarantined), 18)
        self.assertTrue(report["semantic_fact_lexical_leakage"])
        self.assertFalse(report["payload_has_raw_claim_text_field"])
        self.assertFalse(report["quotes_in_assembled_article"])
        self.assertEqual(report["headline_qa_issues"], [])
        self.assertTrue(report["dek_truncation_issues"])
        self.assertFalse(report["historical_artifacts_modified"])
        native = json.loads((V32_RUN / "native.json").read_text(encoding="utf-8"))
        self.assertIn("could push", native["paragraphs"][0]["text"])


if __name__ == "__main__":
    unittest.main()


