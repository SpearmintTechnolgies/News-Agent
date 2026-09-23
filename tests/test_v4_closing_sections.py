"""Grounded Conclusion / What Happens Next + FAQ closing sections."""

from __future__ import annotations

import unittest

from newsagent_v2.article.writer.v4.atomic_grounding import AtomicProposition, AuthorizedPropositionSet
from newsagent_v2.article.writer.v4.closing_sections import (
    FAQ_HEADING,
    CONCLUSION_HEADING,
    append_grounded_closing_sections,
    body_has_closing_sections,
    detect_closing_sections,
)
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.verify import VerificationReport
from newsagent_v2.article.writer.v4.writer import V4NativeArticle, build_v4_writer_messages
from newsagent_v2.article.writer.v4.packet import build_writer_evidence_packet


def _props() -> tuple[AtomicProposition, ...]:
    return (
        AtomicProposition(
            prop_id="P01",
            parent_claim_id="C01",
            text="Deutsche Bank is awaiting regulatory approval for crypto custody.",
            subject="Deutsche Bank",
            attribution="",
            modality="awaiting",
        ),
        AtomicProposition(
            prop_id="P02",
            parent_claim_id="C02",
            text="Initial supported asset includes Bitcoin.",
            subject="Bitcoin",
        ),
        AtomicProposition(
            prop_id="P03",
            parent_claim_id="C03",
            text="Initial supported asset includes Ether.",
            subject="Ether",
        ),
        AtomicProposition(
            prop_id="P04",
            parent_claim_id="C04",
            text="Onboarding would begin after licenses are granted.",
            subject="Onboarding",
            modality="would begin",
        ),
        AtomicProposition(
            prop_id="P05",
            parent_claim_id="C05",
            text="A company spokesperson confirmed the timeline remains contingent on approval.",
            attribution="spokesperson",
            modality="contingent",
        ),
    )


class ClosingSectionsTests(unittest.TestCase):
    def test_appends_conclusion_and_faqs_from_authorized_facts(self) -> None:
        body = (
            "Deutsche Bank is awaiting regulatory approval for crypto custody. "
            "Market desks noted the filing without adding further detail."
        )
        new_body, meta = append_grounded_closing_sections(
            body,
            authorized_propositions=_props(),
            article_type="FULL_ARTICLE",
        )
        self.assertTrue(meta["appended"])
        self.assertIn(CONCLUSION_HEADING, new_body)
        self.assertIn(FAQ_HEADING, new_body)
        self.assertGreaterEqual(meta["faq_count"], 2)
        self.assertLessEqual(meta["faq_count"], 4)
        self.assertIn("Initial supported asset includes Bitcoin.", new_body)
        self.assertNotIn("could reshape the industry", new_body.lower())

    def test_skips_when_already_present(self) -> None:
        body = (
            "Deutsche Bank is awaiting regulatory approval for crypto custody.\n\n"
            f"## {CONCLUSION_HEADING}\n\nAlready here.\n\n## {FAQ_HEADING}\n\n**Q:** x\n**A:** y"
        )
        new_body, meta = append_grounded_closing_sections(
            body, authorized_propositions=_props()
        )
        self.assertFalse(meta["appended"])
        self.assertEqual(meta["skipped_reason"], "already_present")
        self.assertEqual(new_body, body)

    def test_skips_limited_article_type(self) -> None:
        body = "Deutsche Bank is awaiting regulatory approval for crypto custody."
        new_body, meta = append_grounded_closing_sections(
            body,
            authorized_propositions=_props(),
            article_type="LIMITED_DEPTH_BRIEF",
        )
        self.assertFalse(meta["appended"])
        self.assertEqual(meta["skipped_reason"], "limited_article_type")
        self.assertEqual(new_body, body)

    def test_answers_are_authorized_propositions_only(self) -> None:
        body = "Market desks noted a custody filing."
        new_body, meta = append_grounded_closing_sections(
            body, authorized_propositions=_props()
        )
        self.assertTrue(meta["appended"])
        authorized = {p.text.rstrip(".") + "." for p in _props()}
        # Extract FAQ answers
        for line in new_body.splitlines():
            if line.startswith("**A:**"):
                answer = line.replace("**A:**", "", 1).strip()
                self.assertIn(answer, authorized)

    def test_assemble_includes_closing_sections(self) -> None:
        claims = tuple(
            LedgerClaim(f"C{i:02d}", p.text, "fact", ("e1",))
            for i, p in enumerate(_props(), start=1)
        )
        ledgers = EvidenceLedgers(event_id="evt-close", claims=claims, quotes=())
        native = V4NativeArticle(
            headline="Deutsche Bank awaits crypto custody approval",
            dek="Approval pending.",
            article_body=(
                "Deutsche Bank is awaiting regulatory approval for crypto custody. "
                "A company spokesperson confirmed the timeline remains contingent on approval."
            ),
            seo_title="Deutsche Bank custody",
            meta_description="Approval pending",
            slug="deutsche-bank-custody",
        )
        article = assemble_v4_article(
            event_id="evt-close",
            native=native,
            ledgers=ledgers,
            article_input={"article_type": "FULL_ARTICLE", "category": "other"},
            report=VerificationReport(ok=True, rows=()),
        )
        self.assertTrue(body_has_closing_sections(article["article_body"]))
        self.assertTrue(article.get("closing_sections", {}).get("appended"))

    def test_writer_prompt_mentions_closing_sections(self) -> None:
        ledgers = EvidenceLedgers(
            event_id="evt-close",
            claims=(
                LedgerClaim("C01", "Deutsche Bank is awaiting regulatory approval for crypto custody.", "fact", ("e1",)),
            ),
            quotes=(),
        )
        packet = build_writer_evidence_packet(
            event_id="evt-close", ledgers=ledgers, story_topic="Custody"
        )
        messages = build_v4_writer_messages(packet)
        blob = "\n".join(m["content"] for m in messages)
        self.assertIn("Conclusion / What Happens Next", blob)
        self.assertIn("FAQs", blob)


if __name__ == "__main__":
    unittest.main()
