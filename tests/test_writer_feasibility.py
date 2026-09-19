"""Pre-writer feasibility vs final QA. No live providers."""

from __future__ import annotations

import unittest
from dataclasses import replace

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.controlled.capacity import (
    CLASS_INSUFFICIENT,
    EvidenceCapacity,
    article_input_for_ledgers,
)
from newsagent_v2.article.writer.controlled.groq_oss20 import parse_controlled_v3_native
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.controlled.renderer_contract import (
    CROSS_PARAGRAPH_FACT,
    UNKNOWN_FACT_ID,
    validate_sentence_declarations,
)
from newsagent_v2.article.writer.controlled.writer_feasibility import evaluate_writer_feasibility
from newsagent_v2.article.writer.evidence_ledger import LedgerClaim, build_evidence_ledgers
from newsagent_v2.control.make_recovery import assess_make_candidate
from tests.test_controlled_writer_v3 import _rich_story


def _capacity(
    *,
    safe_max: int,
    claims: int = 12,
    capacity_class: str = CLASS_INSUFFICIENT,
) -> EvidenceCapacity:
    return EvidenceCapacity(
        claim_count=claims,
        quote_count=0,
        numeric_fact_count=2,
        attribution_count=1,
        source_count=1,
        estimated_safe_word_min=max(0, safe_max - 30),
        estimated_safe_word_max=safe_max,
        sufficient_for_article=False,
        capacity_class=capacity_class,
        min_planned_safe_words=430,
        safety_margin_words=80,
        paragraph_loss_tolerance=1,
        minimum_surviving_words=safe_max - 50,
        reason=f"safe_words={safe_max}_below_hard_minimum=350",
    )


def _thin_story() -> dict:
    return {
        "event_id": "thin-001",
        "article_input": {
            "event_id": "thin-001",
            "evidence": [
                {
                    "url": "https://example.invalid/rss",
                    "title": "Short tip",
                    "summary": "A firm said it may review files.",
                    "extracted_text": "",
                    "factual_snippets": [],
                }
            ],
        },
    }


def _rich_researched_like_024() -> dict:
    # ~320 extracted words + titles/snippets â†’ volume like event-024 after research.
    body = (
        "Deutsche Bank received a regulatory nod for institutional crypto custody services. "
        "Supervisors said the approval covers safekeeping of digital assets for corporate clients. "
        "The bank confirmed it will launch the offering after completing operational controls. "
        "Officials stated the framework includes capital, audit, and segregation requirements. "
        "Market desks reported that several funds had already inquired about onboarding timelines. "
        "Compliance staff said client assets will remain bankruptcy-remote under the approved structure. "
        "Analysts noted the move follows earlier European custody pilots announced last year. "
        "A spokesperson said pricing and eligible asset lists will be published in a later circular. "
        "Regulators emphasized ongoing reporting duties and third-party audit access. "
        "The bank said technology partners were selected after a competitive review. "
        "Institutional investors said they want clearer settlement rails before allocating size. "
        "Legal counsel confirmed the license does not authorize proprietary trading in crypto. "
        "Operations teams said cold storage and multi-party controls are mandatory. "
        "The announcement listed contact channels for qualified institutional counterparties. "
        "Management said the first client cohort is expected within the current fiscal year. "
    ) * 2
    return {
        "event_id": "event-024-like",
        "article_input": {
            "event_id": "event-024-like",
            "evidence": [
                {
                    "url": "https://cointelegraph.example/news/deutsche-bank-custody",
                    "title": "Deutsche Bank gets regulatory nod for institutional crypto custody",
                    "summary": "Supervisors approved a custody framework for digital assets.",
                    "extracted_text": body,
                    "factual_snippets": body.split(". ")[:12],
                    "source": "cointelegraph",
                    "source_role": "newsroom",
                }
            ],
        },
    }


class WriterFeasibilityTests(unittest.TestCase):
    def test_thin_rss_packet_is_writer_infeasible(self) -> None:
        story = _thin_story()
        assessed = assess_make_candidate(story)
        self.assertFalse(assessed["eligible"])
        self.assertFalse(assessed["writer_feasible"])
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_event024_like_is_writer_feasible_even_if_safe_347(self) -> None:
        story = _rich_researched_like_024()
        pack = article_input_for_ledgers(story["article_input"])
        ledgers = build_evidence_ledgers(pack)
        capacity = _capacity(safe_max=347, claims=max(6, len(ledgers.claims)))
        # Force advisory 347 while evidence volume is rich.
        feasibility = evaluate_writer_feasibility(
            article_input=story["article_input"],
            ledgers=ledgers if len(ledgers.claims) >= 6 else replace(
                ledgers,
                claims=tuple(
                    LedgerClaim(
                        claim_id=f"C{i:02d}",
                        text=f"Institutional custody fact number {i} includes capital and audit controls for clients.",
                        claim_type="fact",
                        evidence_ids=("s1",),
                    )
                    for i in range(1, 13)
                ),
            ),
            capacity=capacity,
        )
        self.assertTrue(feasibility["writer_feasible"])
        assessed = assess_make_candidate(story)
        self.assertTrue(assessed["writer_feasible"])
        self.assertTrue(assessed["eligible"])

    def test_final_qa_still_fails_below_350(self) -> None:
        from newsagent_v2.article.qa.policy import check_depth

        article = {"article_body": " ".join(["custody"] * 347)}
        story = _rich_researched_like_024()
        issues, _metrics = check_depth(article, story["article_input"], NORMAL_ARTICLE_POLICY)
        codes = [str(item.get("code") or "") for item in issues]
        self.assertIn("below_article_minimum_length", codes)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_final_qa_length_can_pass_at_350_plus(self) -> None:
        from newsagent_v2.article.qa.policy import check_depth

        body = (
            "Deutsche Bank received regulatory approval for institutional crypto custody. "
            "Supervisors said the framework covers safekeeping for corporate clients. "
            "The bank confirmed operational controls, capital rules, and audit access. "
            "A spokesperson said eligible assets and pricing will be published later. "
            "Legal counsel confirmed the license does not authorize proprietary trading. "
        )
        article_body = (body * 20).strip()
        self.assertGreaterEqual(len(article_body.split()), 350)
        article = {"article_body": article_body}
        story = _rich_researched_like_024()
        issues, _metrics = check_depth(article, story["article_input"], NORMAL_ARTICLE_POLICY)
        self.assertFalse(any(i.get("code") == "below_article_minimum_length" for i in issues))
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_authorized_cross_paragraph_fact_not_invalid(self) -> None:
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
                            "text": "Northwind Payments disclosed a spoofed-email customer-file review.",
                            "fact_ids_used": [p2.allowed_claim_ids[0]],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues = validate_sentence_declarations(native, plan)
        self.assertFalse(any(item["code"] == CROSS_PARAGRAPH_FACT for item in issues))
        self.assertTrue(parse_controlled_v3_native(native, plan).ok)

    def test_unknown_fact_id_still_invalid(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        p1 = plan.paragraph_plans[0]
        native = {
            "paragraphs": [
                {
                    "paragraph_id": p1.paragraph_id,
                    "sentences": [
                        {
                            "sentence_id": "s1",
                            "text": "Someone invented an unsupported market consequence today.",
                            "fact_ids_used": ["NOT_A_REAL_FACT"],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues = validate_sentence_declarations(native, plan)
        self.assertTrue(any(item["code"] == UNKNOWN_FACT_ID for item in issues))
        self.assertTrue(parse_controlled_v3_native(native, plan).invalid_output)

    def test_attribution_locality_still_enforced(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        p1 = plan.paragraph_plans[0]
        p2 = plan.paragraph_plans[1]
        local = replace(p1, relationship="ATTRIBUTION")
        plan = replace(plan, paragraph_plans=(local,) + plan.paragraph_plans[1:])
        native = {
            "paragraphs": [
                {
                    "paragraph_id": local.paragraph_id,
                    "sentences": [
                        {
                            "sentence_id": "s1",
                            "text": "Officials said the review covers retail records.",
                            "fact_ids_used": [p2.allowed_claim_ids[0]],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues = validate_sentence_declarations(native, plan)
        self.assertTrue(any(item["code"] == CROSS_PARAGRAPH_FACT for item in issues))


if __name__ == "__main__":
    unittest.main()


