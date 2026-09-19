from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.writer.controlled.assembler import assemble_canonical_article
from newsagent_v2.article.writer.controlled.paragraph import validate_paragraph
from newsagent_v2.article.writer.controlled.pipeline import compile_controlled_article
from newsagent_v2.article.writer.controlled.plan import ArticlePlan, plan_article
from newsagent_v2.article.writer.controlled.proposition import (
    FORBIDDEN_INFERRED_RELATIONSHIPS,
    RELATIONSHIP_ATTRIBUTION,
    RELATIONSHIP_NONE,
    RELATIONSHIP_SEQUENCE,
    assign_paragraph_relationship,
    proposition_frame_from_claim,
    realize_proposition_frame,
    relationship_is_forbidden_inference,
)
from newsagent_v2.article.writer.controlled.realization import (
    REALIZATION_INVALID,
    classify_article_sentences,
    editorial_is_invalid,
    sentence_realization_issues,
)
from newsagent_v2.article.writer.controlled.renderer import (
    RenderedParagraph,
    ScriptedProseRenderer,
    controlled_renderer_payload,
    renderer_fact_ids,
)
from newsagent_v2.article.writer.controlled.renderer_contract import (
    UNAUTHORIZED_RELATIONSHIP,
    validate_sentence_declarations,
)
from newsagent_v2.article.writer.controlled.semantic import semantic_adds_no_facts, semantic_fact_from_claim
from newsagent_v2.article.writer.controlled.v331_forensics import replay_v33_prose
from newsagent_v2.article.writer.evidence_ledger import LedgerClaim, LedgerQuote, build_evidence_ledgers
from tests.test_controlled_writer_v3 import _rich_story, _sentence
from tests.test_controlled_writer_v32 import _ledgers, _plan

REPO = Path(__file__).resolve().parents[1]
V33_LIVE = (
    REPO
    / "benchmarks"
    / "writer_bakeoff"
    / "controlled_v33_fresh"
    / "live_runs"
    / "20260916T093822Z"
    / "groq_gpt_oss_20b_controlled_writer_v33"
)

C13_TEXT = (
    "(Jesse Hamilton/CoinDesk) Summary The crypto Clarity Act failed to get the 60 votes "
    "needed to advance in the U.S. Senate after months of intense negotiations led to a "
    "49-50 vote that didnâ€™t manage to even win a majority."
)


class ControlledWriterV331Tests(unittest.TestCase):
    def test_proposition_frame_contains_no_raw_source_sentence(self) -> None:
        claim = LedgerClaim("C13", C13_TEXT, "fact", ("event-008-e02",))
        frame = proposition_frame_from_claim(claim).as_dict()
        blob = json.dumps(frame)
        self.assertNotIn(claim.text, blob)
        self.assertNotIn(
            "needed to advance in the U.S. Senate after months of intense negotiations",
            blob,
        )

    def test_proposition_frame_preserves_factual_roles_and_anchors(self) -> None:
        claim = LedgerClaim("C13", C13_TEXT, "fact", ("event-008-e02",))
        frame = proposition_frame_from_claim(claim)
        self.assertEqual(frame.fact_id, "C13")
        self.assertTrue(frame.subject)
        self.assertEqual(frame.predicate, "failed")
        self.assertIn("60", frame.numbers)
        self.assertTrue(any("Clarity" in item for item in frame.legal_or_official_names))
        fact = semantic_fact_from_claim(claim)
        self.assertTrue(semantic_adds_no_facts(fact, claim))
        joined = " ".join(
            [frame.subject, frame.predicate, frame.complement, " ".join(frame.qualifiers)]
        )
        self.assertNotIn(
            "needed to advance in the u.s. senate after months of intense negotiations",
            joined.lower(),
        )

    def test_one_fact_realizes_one_grammatical_sentence(self) -> None:
        claim = LedgerClaim(
            "C01",
            "Industry executives said the vote would not halt regulatory work by the SEC and CFTC in the United States.",
            "fact",
            ("e1",),
        )
        sentence = realize_proposition_frame(proposition_frame_from_claim(claim))
        self.assertTrue(sentence.endswith("."))
        self.assertFalse(sentence_realization_issues(sentence, relationship=RELATIONSHIP_NONE))
        self.assertNotEqual(sentence.strip().lower(), claim.text.strip().lower())

    def test_unsupported_relationships_remain_forbidden(self) -> None:
        for name in FORBIDDEN_INFERRED_RELATIONSHIPS:
            self.assertTrue(relationship_is_forbidden_inference(name))
        claims = [LedgerClaim("C01", _sentence(1), "fact", ("e1",))]
        self.assertEqual(
            assign_paragraph_relationship(editorial_purpose="lead", quote_ids=(), claims=claims),
            RELATIONSHIP_NONE,
        )

    def test_none_forces_independent_factual_treatment(self) -> None:
        plan = _plan(("C01", "C02"))
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
                            "text": f"{_sentence(1)} {_sentence(2)}",
                            "fact_ids_used": ["C01", "C02"],
                            "quote_ids_used": [],
                        }
                    ],
                }
            ]
        }
        issues = validate_sentence_declarations(native, article_plan)
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_explicit_attribution_and_sequence_allowed(self) -> None:
        self.assertEqual(
            assign_paragraph_relationship(
                editorial_purpose="key_facts",
                quote_ids=(),
                claims=[LedgerClaim("C01", _sentence(1), "fact", ("e1",))],
            ),
            RELATIONSHIP_SEQUENCE,
        )
        self.assertEqual(
            assign_paragraph_relationship(
                editorial_purpose="lead",
                quote_ids=("Q01",),
                claims=[LedgerClaim("C01", _sentence(1), "fact", ("e1",))],
            ),
            RELATIONSHIP_ATTRIBUTION,
        )

    def test_unsupported_causal_and_prediction_rejected(self) -> None:
        causal = "Northwind Payments disclosed review item 1 because markets will boom after the filing."
        pred = "Northwind Payments disclosed review item 1 and is expected to transform retail files."
        self.assertTrue(sentence_realization_issues(causal, relationship=RELATIONSHIP_NONE))
        self.assertTrue(sentence_realization_issues(pred, relationship=RELATIONSHIP_NONE))

    def test_fragments_and_dangling_phrases_rejected(self) -> None:
        self.assertTrue(sentence_realization_issues("uncertainty push investment development."))
        self.assertTrue(sentence_realization_issues("The company disclosed records such as the"))
        self.assertTrue(sentence_realization_issues("and the filing continued without a verb"))

    def test_malformed_headline_and_truncated_dek_rejected(self) -> None:
        self.assertTrue(editorial_is_invalid("Vote Halt Regulatory Work SEC CFTC", field="headline"))
        self.assertTrue(
            editorial_is_invalid(
                "uncertainty push investment development jurisdictions European Union",
                field="dek",
            )
        )

    def test_plain_grammatical_prose_accepted(self) -> None:
        plain = "Northwind Payments disclosed review item 1 covering 12001 customer records."
        self.assertFalse(sentence_realization_issues(plain, relationship=RELATIONSHIP_NONE))

    def test_realization_invalid_never_assembled(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        para = plan.paragraph_plans[0]
        good = ledgers.claim_by_id()[para.required_claim_ids[0]].text
        fragment = "uncertainty push investment development jurisdictions."
        validated = validate_paragraph(
            RenderedParagraph(paragraph_id=para.paragraph_id, text=f"{good} {fragment}"),
            para,
            ledgers,
        )
        article = assemble_canonical_article(
            plan=plan,
            ledgers=ledgers,
            article_input=story["article_input"],
            validated=[validated],
        )
        self.assertNotIn("uncertainty push investment", article["article_body"])
        self.assertTrue(any(item["code"] == REALIZATION_INVALID for item in validated.issues))

    def test_grounding_similarity_quarantine_minimum_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        story = _rich_story()
        compiled = compile_controlled_article(story)
        self.assertIsNotNone(compiled.article)
        assert compiled.article is not None
        qa = run_article_qa(compiled.article, story["article_input"], article_mode="normal")
        self.assertIn("qa_passed", qa)

    def test_scripted_fragment_does_not_enter_body(self) -> None:
        story = _rich_story()
        compiled = compile_controlled_article(
            story,
            renderer=ScriptedProseRenderer(
                texts={},
                headline="Halt Vote SEC CFTC",
                dek="uncertainty push investment",
            ),
        )
        self.assertIsNotNone(compiled.article)
        assert compiled.article is not None
        self.assertNotEqual(compiled.article["headline"], "Halt Vote SEC CFTC")
        self.assertNotIn("uncertainty push", compiled.article["dek"])

    def test_offline_replay_classifies_persisted_v33_prose(self) -> None:
        article = json.loads((V33_LIVE / "article.json").read_text(encoding="utf-8"))
        report = classify_article_sentences(
            headline=article["headline"],
            dek=article["dek"],
            body=(V33_LIVE / "article_body.txt").read_text(encoding="utf-8"),
        )
        self.assertGreater(sum(report["counts"].values()), 0)
        replay_v33_prose(V33_LIVE)

    def test_payload_exposes_frames_not_claim_text(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        payload = controlled_renderer_payload(plan, ledgers)
        blob = json.dumps(payload)
        self.assertIn("proposition_frames", blob)
        self.assertIn("relationship", blob)
        self.assertNotIn("allowed_semantic_facts", blob)
        for claim in ledgers.claims:
            if len(claim.text.split()) >= 18:
                self.assertNotIn(claim.text, blob)

    def test_renderer_projects_only_authorized_plan_facts(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        unused_text = "ZetaCorp privately filed 999 unused widgets after an internal memo nobody selected."
        unused_quote = "This unused quote must not reach the renderer payload."
        extra = replace(
            ledgers,
            claims=ledgers.claims
            + (LedgerClaim("CUNUSED", unused_text, "fact", ("e-unused",)),),
            quotes=ledgers.quotes + (LedgerQuote("QUNUSED", unused_quote, "Unused Speaker", "e-unused"),),
        )
        self.assertEqual(len(extra.claims), len(ledgers.claims) + 1)
        plan = plan_article(ledgers, story["article_input"])
        projected = renderer_fact_ids(plan)
        self.assertTrue(projected <= {row.claim_id for row in extra.claims})
        for para in plan.paragraph_plans:
            for fact_id in para.required_claim_ids:
                self.assertIn(fact_id, projected)
        payload = controlled_renderer_payload(plan, extra)
        blob = json.dumps(payload)
        self.assertNotIn("CUNUSED", blob)
        self.assertNotIn(unused_text, blob)
        self.assertNotIn("QUNUSED", blob)
        self.assertNotIn(unused_quote, blob)
        self.assertNotIn("allowed_semantic_facts", blob)
        payload_ids = {
            str(frame.get("fact_id"))
            for para in payload["paragraph_plans"]
            for frame in para["proposition_frames"]
        }
        self.assertEqual(payload_ids, projected)
        self.assertNotIn("CUNUSED", projected)
        self.assertTrue({"CUNUSED"} <= ({row.claim_id for row in extra.claims} - projected))
        self.assertEqual(len(extra.quotes), len(ledgers.quotes) + 1)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)


