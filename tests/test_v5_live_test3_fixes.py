"""Regressions for Live Test #3 (evt-1803c9c4) closing/depth/enrichment fixes."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from newsagent_v2.article.qa.grounding import (
    check_body_claim_coverage,
    is_faq_question_or_structural_heading,
)
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, SEVERITY_WARNING
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.atomic_grounding import AtomicProposition
from newsagent_v2.article.writer.v4.closing_sections import (
    CONCLUSION_HEADING,
    FAQ_HEADING,
    append_grounded_closing_sections,
    detect_closing_sections,
)
from newsagent_v2.article.writer.v4.evidence_depth import (
    CAPACITY_RICH,
    FULL_RANGE,
    assess_evidence_capacity,
    check_v4_article_depth,
)
from newsagent_v2.article.writer.v4.event_research import EventResearchResult
from newsagent_v2.article.writer.v4.expand import realize_body_word_target
from newsagent_v2.article.writer.v4.factbank import FactBank, FactProposition, fact_bank_to_writer_packet
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import ScriptedV4Writer, V4NativeArticle


def _props() -> tuple[AtomicProposition, ...]:
    return (
        AtomicProposition(
            prop_id="P01",
            parent_claim_id="C01",
            text="Bitcoin recovered from Asian-session lows near $85,000.",
            subject="Bitcoin",
        ),
        AtomicProposition(
            prop_id="P02",
            parent_claim_id="C02",
            text="Falling oil prices supported risk appetite across markets.",
            subject="oil",
        ),
        AtomicProposition(
            prop_id="P03",
            parent_claim_id="C03",
            text="U.S.-listed spot bitcoin ETFs attracted nearly $1 billion in inflows.",
            subject="ETFs",
        ),
        AtomicProposition(
            prop_id="P04",
            parent_claim_id="C04",
            text="Bitcoin recently traded near $86,155.",
            subject="Bitcoin",
        ),
    )


def _pad(body: str, n: int) -> str:
    filler = (
        "Bitcoin recovered from Asian-session lows near $85,000. "
        "Falling oil prices supported risk appetite across markets. "
        "U.S.-listed spot bitcoin ETFs attracted nearly $1 billion in inflows. "
    )
    while len(body.split()) < n:
        body = (body + " " + filler).strip()
    return body


def _rich_bank() -> FactBank:
    templates = [
        "Deutsche Bank is awaiting regulatory approval for crypto custody.",
        "Initial supported asset includes Bitcoin.",
        "Initial supported asset includes Ether.",
        "Initial supported assets include selected stablecoins.",
        "Future planned coverage includes tokenized financial instruments.",
        "The custody unit will serve institutional clients first.",
        "A company spokesperson confirmed the timeline remains contingent on approval.",
        "Regulators have not issued a final decision on the application.",
        "The bank said onboarding would begin after licenses are granted.",
        "Compliance reviews cover wallet segregation and audit trails.",
        "Bitcoin and Ether remain the first listed digital assets.",
        "Selected USD stablecoins are under internal risk review.",
    ]
    props = []
    for i, text in enumerate(templates, start=1):
        props.append(
            FactProposition(
                proposition_id=f"P{i:02d}",
                text=text,
                subject="Deutsche Bank" if i == 1 else "",
                numbers=("1",) if "Bitcoin" in text else (),
                attribution="spokesperson" if "spokesperson" in text else "",
                primary_source_support=i <= 3,
            )
        )
    return FactBank(event_id="rich", propositions=tuple(props))


class LiveTest3ClosingIdempotencyTests(unittest.TestCase):
    def test_writer_already_has_conclusion_and_faq_no_duplicate(self) -> None:
        body = (
            "Bitcoin recovered from Asian-session lows near $85,000. "
            "Falling oil prices supported risk appetite across markets.\n\n"
            "**Conclusion / What Happens Next**\n\n"
            "Bitcoin remains positioned as traders monitor ETF inflows.\n\n"
            "**FAQs**\n\n"
            "**What drove bitcoin's recent price recovery?** "
            "Falling oil prices supported risk appetite across markets. "
            "**How much did U.S. bitcoin ETFs attract on Monday?** "
            "U.S.-listed spot bitcoin ETFs attracted nearly $1 billion in inflows."
        )
        new_body, meta = append_grounded_closing_sections(
            body, authorized_propositions=_props(), article_type="FULL_ARTICLE"
        )
        self.assertFalse(meta["appended"])
        self.assertEqual(meta["skipped_reason"], "already_present")
        self.assertEqual(new_body.count("Conclusion"), body.count("Conclusion"))
        self.assertEqual(new_body, body)

    def test_conclusion_only_adds_faq_not_second_conclusion(self) -> None:
        body = (
            "Bitcoin recovered from Asian-session lows near $85,000.\n\n"
            f"## {CONCLUSION_HEADING}\n\n"
            "Falling oil prices supported risk appetite across markets."
        )
        new_body, meta = append_grounded_closing_sections(
            body, authorized_propositions=_props(), article_type="FULL_ARTICLE"
        )
        self.assertTrue(meta["appended"])
        self.assertTrue(meta["had_conclusion"])
        self.assertFalse(meta["had_faq"])
        self.assertEqual(new_body.lower().count("conclusion"), 1)
        self.assertIn(FAQ_HEADING, new_body)
        self.assertGreaterEqual(meta["faq_count"], 2)


class LiveTest3DepthPolicyTests(unittest.TestCase):
    def test_grounded_564_rich_not_critical_for_recommended_700(self) -> None:
        research = EventResearchResult(
            pack={}, story={}, independent_sources=3, primary_sources=1
        )
        depth = assess_evidence_capacity(_rich_bank(), research=research)
        self.assertEqual(depth.evidence_capacity, CAPACITY_RICH)
        self.assertEqual(depth.recommended_word_min, FULL_RANGE[0])
        article = {
            "article_body": _pad(
                "Deutsche Bank is awaiting regulatory approval for crypto custody. "
                "Initial supported asset includes Bitcoin.",
                564,
            )
        }
        issues = check_v4_article_depth(article, depth)
        critical = [i for i in issues if i.get("severity") == SEVERITY_CRITICAL]
        self.assertEqual(critical, [])
        self.assertFalse(any(i.get("code") == "below_article_type_minimum" for i in issues))
        warnings = [i for i in issues if i.get("severity") == SEVERITY_WARNING]
        self.assertTrue(any(i.get("code") == "below_editorial_recommended_range" for i in warnings))

    def test_under_500_still_critical(self) -> None:
        research = EventResearchResult(
            pack={}, story={}, independent_sources=3, primary_sources=1
        )
        depth = assess_evidence_capacity(_rich_bank(), research=research)
        article = {"article_body": _pad("Deutsche Bank is awaiting regulatory approval for crypto custody.", 450)}
        issues = check_v4_article_depth(article, depth)
        self.assertTrue(any(i.get("code") == "below_absolute_publication_minimum" for i in issues))


class LiveTest3FaqGroundingTests(unittest.TestCase):
    def test_faq_questions_exempt_answers_still_checked(self) -> None:
        self.assertTrue(is_faq_question_or_structural_heading("**Q:** What did reporting establish?"))
        self.assertTrue(is_faq_question_or_structural_heading("## FAQs"))
        self.assertTrue(is_faq_question_or_structural_heading("**What drove bitcoin's recent price recovery?**"))
        self.assertFalse(
            is_faq_question_or_structural_heading(
                "**A:** U.S.-listed spot bitcoin ETFs attracted nearly $1 billion in inflows."
            )
        )
        claims = [
            LedgerClaim("C01", "Bitcoin recovered from Asian-session lows near $85,000.", "fact", ("e1",)),
            LedgerClaim("C02", "Falling oil prices supported risk appetite across markets.", "fact", ("e1",)),
            LedgerClaim(
                "C03",
                "U.S.-listed spot bitcoin ETFs attracted nearly $1 billion in inflows.",
                "fact",
                ("e1",),
            ),
        ]
        article = {
            "architecture": "v4",
            "generation_notes": "v4_natural_prose",
            "event_id": "evt-1803c9c4",
            "headline": "Bitcoin recovers from Asian-session lows",
            "dek": "Oil supports risk appetite.",
            "claims": [
                {"claim_id": c.claim_id, "text": c.text, "claim_type": "fact", "evidence_ids": list(c.evidence_ids)}
                for c in claims
            ],
            "article_body": (
                "Bitcoin recovered from Asian-session lows near $85,000. "
                "Falling oil prices supported risk appetite across markets.\n\n"
                "## FAQs\n\n"
                "**Q:** What did reporting establish?\n"
                "**A:** U.S.-listed spot bitcoin ETFs attracted nearly $1 billion in inflows.\n"
                "**Q:** What else did sources establish?\n"
                "**A:** Bitcoin recovered from Asian-session lows near $85,000."
            ),
        }
        issues, _metrics = check_body_claim_coverage(article)
        question_hits = [
            i for i in issues if str(i.get("sentence") or "").strip().startswith("**Q:**")
        ]
        self.assertEqual(question_hits, [])


class LiveTest3EmptyEnrichmentContinuesRegenTests(unittest.TestCase):
    def test_zero_word_enrichment_continues_to_regeneration(self) -> None:
        research = EventResearchResult(pack={}, story={}, independent_sources=2)
        medium_props = _rich_bank().propositions[:8]
        bank = FactBank(event_id="m", propositions=medium_props)
        depth = assess_evidence_capacity(bank, research=research)
        filler = "Local desks tracked flows without naming additional counterparties. "
        body = "Deutsche Bank is awaiting regulatory approval for crypto custody. "
        while len(body.split()) < 320:
            body = (body + filler).strip()
        native = V4NativeArticle(
            headline="Deutsche Bank is awaiting regulatory approval for crypto custody",
            dek="Custody",
            article_body=body,
            seo_title="Custody",
            meta_description="Custody",
            slug="custody",
        )
        packet = fact_bank_to_writer_packet(bank)
        ledgers = bank.to_ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        from newsagent_v2.article.writer.v4.expand import UnusedEvidenceSet
        # Force a non-empty unused set (packet facts often lack provenance in fixtures).
        forced_unused = UnusedEvidenceSet(
            facts=tuple(packet.authorized_facts[1:4]),
            represented_ids=(packet.authorized_facts[0].id,),
        )
        regen_body = _pad(
            "Deutsche Bank is awaiting regulatory approval for crypto custody. "
            "Initial supported asset includes Bitcoin. "
            "Initial supported asset includes Ether. "
            "The custody unit will serve institutional clients first.",
            520,
        )
        writer = ScriptedV4Writer(
            native.as_dict(),
            expansion_text="",  # empty enrichment → 0 validated words
            regeneration_payload={**native.as_dict(), "article_body": regen_body},
        )
        with patch(
            "newsagent_v2.article.writer.v4.expand.build_unused_evidence_set",
            return_value=forced_unused,
        ):
            out, _r, expansion = realize_body_word_target(
                native,
                packet=packet,
                ledgers=ledgers,
                article_input={"evidence": []},
                report=report,
                writer=writer,
                depth=depth,
            )
        self.assertGreaterEqual(writer.expansion_calls, 1)
        self.assertEqual(writer.regeneration_calls, 1)
        self.assertEqual(expansion.mode, "regeneration")
        # Empty enrichment must not block regeneration continuation.
        self.assertIsNotNone(expansion.unused_before)




if __name__ == "__main__":
    unittest.main()
