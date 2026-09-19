"""Regression tests for V4 500-word capability V3 downstream QA/repair fixes."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.schema import check_schema
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.capability_500_v3 import (
    article_input_from_packet_v3,
    ledgers_from_packet_v3,
    load_event_011_provenance_map,
    verify_event_030_untouched,
)
from newsagent_v2.article.writer.v4.packet import AuthorizedFact, WriterEvidencePacket
from newsagent_v2.article.writer.v4.repair import (
    is_boilerplate_proposition,
    is_complete_newsroom_sentence,
    is_sentence_fragment,
    newsroom_sentence_from_authorized,
    paragraph_integrity_ok,
    repair_unsupported_propositions,
    soft_trim_article_body,
)
from newsagent_v2.article.writer.v4.verify import (
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    verify_v4_native,
)
from newsagent_v2.article.writer.v4.writer import V4NativeArticle

REPO = Path(__file__).resolve().parents[1]
PROTECTED_HASH = "66a948580b2d1f5bce2518061a83c6130358437f6c3b4a329597185bbb22213a"


def _packet_and_ledgers() -> tuple[WriterEvidencePacket, EvidenceLedgers]:
    facts = (
        AuthorizedFact(
            id="C1",
            proposition="Deutsche Bank is awaiting final regulatory approval for custody.",
            numbers=(),
            provenance=("e01",),
        ),
        AuthorizedFact(
            id="C2",
            proposition="Sabih Behzad revealed that Deutsche Bank was considering stablecoins.",
            numbers=(),
            provenance=("e01",),
        ),
        AuthorizedFact(
            id="C3",
            proposition="The bank plans to launch institutional custody solutions for Bitcoin.",
            numbers=(),
            provenance=("e01",),
        ),
    )
    packet = WriterEvidencePacket(
        event_id="e",
        story_topic="Deutsche Bank custody",
        authorized_facts=facts,
    )
    ledgers = EvidenceLedgers(
        event_id="e",
        claims=tuple(
            LedgerClaim(
                claim_id=f.id,
                text=f.proposition,
                claim_type="fact",
                evidence_ids=f.provenance,
            )
            for f in facts
        ),
        quotes=(),
    )
    return packet, ledgers


class TestV4500CapabilityV3(unittest.TestCase):
    def test_supported_sentence_not_rewritten(self) -> None:
        packet, ledgers = _packet_and_ledgers()
        supported = (
            "Deutsche Bank is awaiting final regulatory approval for custody. "
            "The bank plans to launch institutional custody solutions for Bitcoin."
        )
        native = V4NativeArticle(
            headline="Deutsche Bank awaits approval",
            dek="Deutsche Bank is awaiting final regulatory approval for custody.",
            article_body=supported
            + " This move positions Deutsche Bank among other major institutions.",
            seo_title="x",
            meta_description="y",
            slug="z",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertFalse(report.ok)
        repaired, actions, _rejected = repair_unsupported_propositions(
            native, packet=packet, ledgers=ledgers, report=report
        )
        # Supported opening sentences must remain intact.
        self.assertIn(
            "Deutsche Bank is awaiting final regulatory approval for custody.",
            repaired.article_body,
        )
        self.assertIn(
            "The bank plans to launch institutional custody solutions for Bitcoin.",
            repaired.article_body,
        )
        # No rewrite action may list a previously SUPPORTED sentence as before=
        supported_texts = {
            row.text for row in report.rows if row.status == STATUS_SUPPORTED
        }
        for action in actions:
            if action.kind == "unsupported_proposition_rewrite":
                self.assertNotIn(action.before, supported_texts)

    def test_unsupported_rewrite_only_targets_unsupported_or_required_ambiguous(
        self,
    ) -> None:
        packet, ledgers = _packet_and_ledgers()
        native = V4NativeArticle(
            headline="Deutsche Bank awaits approval",
            dek="Deutsche Bank is awaiting final regulatory approval for custody.",
            article_body=(
                "Deutsche Bank is awaiting final regulatory approval for custody. "
                "Aliens bought every exchange overnight."
            ),
            seo_title="x",
            meta_description="y",
            slug="z",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        unsupported = {
            row.text
            for row in report.rows
            if row.status in {STATUS_UNSUPPORTED, "AMBIGUOUS"}
        }
        _repaired, actions, _ = repair_unsupported_propositions(
            native, packet=packet, ledgers=ledgers, report=report
        )
        for action in actions:
            if action.kind == "unsupported_proposition_rewrite":
                self.assertIn(action.before, unsupported)

    def test_repair_fragment_rolls_back(self) -> None:
        self.assertTrue(is_sentence_fragment("Attracting $159.9 million on Monday, according to data from Farside."))
        self.assertTrue(
            is_boilerplate_proposition(
                "This news article is produced in accordance with Cointelegraph's Editorial Policy."
            )
        )
        self.assertIsNone(newsroom_sentence_from_authorized("4 high, with stellar down 9.6%"))
        self.assertTrue(
            is_complete_newsroom_sentence(
                "BlackRock's iShares Bitcoin Trust recorded $161.7 million in outflows."
            )
        )

    def test_repair_coherence_regression_rolls_back(self) -> None:
        prior = (
            "U.S. spot bitcoin ETFs shed approximately $450 million on Tuesday after "
            "the Senate procedural vote failed to advance market structure legislation. "
            "The Senate failed to advance the Clarity Act on Tuesday by a clear margin.\n\n"
            "Attention has now shifted to the Federal Reserve rate decision later today "
            "as traders reassess risk appetite across digital asset markets.\n\n"
            "BlackRock's iShares Bitcoin Trust followed with $161.7 million in outflows "
            "while other large issuers also recorded redemptions.\n\n"
            "At the time of writing, bitcoin was trading near $75,700 according to market "
            "data, and remaining authorized timeline details were still being monitored by desks."
        )
        destroyed = (
            "U.S. spot bitcoin ETFs shed approximately $450 million on Tuesday after "
            "the Senate procedural vote failed to advance market structure legislation. "
            "The Senate failed to advance the Clarity Act on Tuesday by a clear margin.\n\n"
            "Attention has now shifted to the Federal Reserve rate decision later today "
            "as traders reassess risk appetite across digital asset markets.\n\n"
            "BlackRock's iShares Bitcoin Trust followed with $161.7 million in outflows "
            "while other large issuers also recorded redemptions.\n\n"
            "A major crypto bill stalled in the Senate."
        )
        self.assertTrue(paragraph_integrity_ok(prior))
        self.assertGreaterEqual(word_count(prior.split("\n\n")[-1]), 25)
        self.assertLess(word_count(destroyed.split("\n\n")[-1]), 15)
        self.assertFalse(paragraph_integrity_ok(destroyed, prior_body=prior))

    def test_trim_preserves_supported_facts_and_complete_sentences(self) -> None:
        paras = []
        paras.append(
            "U.S. spot bitcoin exchange-traded funds shed approximately $450 million on Tuesday, "
            "marking their largest single-day outflow since late June, as legislative setbacks "
            "in Washington dampened market sentiment. The Senate failed to advance the Clarity Act "
            "on Tuesday, falling around 10 votes short of the 60 needed for procedural progress."
        )
        paras.append(
            "According to data from SoSoValue, the 13 U.S.-listed spot bitcoin funds recorded a net "
            "outflow of $450.4 million on Tuesday, a dramatic reversal from the $159.9 million in "
            "inflows attracted just one day prior. Bitcoin traded near $75,679 by midnight UTC after "
            "the vote, while select altcoins experienced sharper corrections over the same window."
        )
        paras.append(
            "Attention has now shifted to the Federal Reserve, which was scheduled to announce its "
            "interest-rate decision later in the day, with an increase having been the market base case. "
            "Bitcoin's 24-hour decline of approximately 1.7% appeared relatively muted compared with "
            "tokens most exposed to U.S. regulatory treatment in recent sessions."
        )
        paras.append(
            "BlackRock's iShares Bitcoin Trust followed with $161.7 million in outflows, while "
            "Grayscale's Bitcoin Trust ETF shed $44.1 million. Additional outflows included "
            "$17.4 million from the ARK 21Shares Bitcoin ETF and $12.4 million from Bitwise."
        )
        paras.append(
            "At the time of writing, bitcoin was trading at approximately $75,700, representing a "
            "2.5% decline over the preceding 24 hours according to CoinMarketCap data. Traders also "
            "watched whether further ETF redemptions would continue into the next session as "
            "regulatory timelines remained unsettled in Congress for the remainder of the year."
        )
        body = "\n\n".join(paras)
        filler_sent = (
            " Market participants continued to reassess positioning after the legislative setback "
            "without introducing new confirmed figures beyond those already stated in the FactBank."
        )
        fat = body
        while word_count(fat) <= 560:
            fat = fat + filler_sent
        self.assertGreater(word_count(fat), 550)
        trimmed, meta = soft_trim_article_body(fat, target_max=550, prefer_max=520)
        self.assertLessEqual(word_count(trimmed), 550)
        self.assertGreaterEqual(word_count(trimmed), 450)
        self.assertIn("$450", trimmed)
        self.assertIn("BlackRock", trimmed)
        self.assertTrue(paragraph_integrity_ok(trimmed, prior_body=fat))
        from newsagent_v2.article.qa.textutil import split_sentences

        for sentence in split_sentences(trimmed):
            if word_count(sentence) >= 5:
                self.assertFalse(is_sentence_fragment(sentence))

    def test_trim_575_into_450_550_without_paragraph_collapse(self) -> None:
        # Synthetic ~575-word 5-paragraph article.
        p1 = " ".join(["Lead"] + ["detail"] * 70) + " statement concludes here."
        p2 = " ".join(["Core"] + ["figure"] * 90) + " totals were confirmed."
        p3 = " ".join(["Context"] + ["background"] * 80) + " remains authorized."
        p4 = " ".join(["Secondary"] + ["outflow"] * 90) + " was recorded."
        p5 = (
            "Closing development covers remaining verified timeline facts. "
            + " ".join(["Next"] * 40)
            + " steps were noted by officials. "
            + " ".join(["Further"] * 30)
            + " process details were already authorized."
        )
        body = "\n\n".join([p1, p2, p3, p4, p5])
        # Force length near 575 by appending complete sentences to last para.
        while word_count(body) < 575:
            body = body.rstrip(".") + ". Additional authorized timeline wording was restated carefully."
        self.assertGreaterEqual(word_count(body), 575)
        trimmed, _meta = soft_trim_article_body(body, target_max=550, prefer_max=520)
        self.assertTrue(450 <= word_count(trimmed) <= 550)
        paras = [p for p in trimmed.split("\n\n") if p.strip()]
        self.assertGreaterEqual(len(paras), 4)
        self.assertGreaterEqual(word_count(paras[-1]), 25)

    def test_valid_evidence_url_preserved_and_legacy_mapping(self) -> None:
        mapping = load_event_011_provenance_map()
        self.assertTrue(mapping)
        for meta in mapping.values():
            self.assertTrue(meta["url"].startswith("https://"))
            self.assertTrue(meta["source"])
        from newsagent_v2.article.writer.v4.capability_500_v2 import load_event_011_packet

        packet, _meta = load_event_011_packet()
        pack = article_input_from_packet_v3(packet, mapping)
        for row in pack["evidence"]:
            self.assertTrue(str(row["url"]).startswith("https://"))
            self.assertFalse(str(row["url"]).startswith("factbank://"))
        for unit in pack["evidence_units"]:
            self.assertTrue(str(unit["url"]).startswith("https://"))
            self.assertTrue(str(unit["evidence_id"]).startswith("event-011-e"))

        ledgers = ledgers_from_packet_v3(packet, mapping)
        from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
        from newsagent_v2.article.writer.v4.verify import VerificationReport

        native = V4NativeArticle(
            headline="Test",
            dek="Test dek for schema.",
            article_body=(
                "U.S. spot bitcoin ETFs shed approximately $450 million on Tuesday. "
                "The Senate failed to advance the Clarity Act on Tuesday."
            ),
            seo_title="Test",
            meta_description="Test dek for schema.",
            slug="test",
        )
        article = assemble_v4_article(
            event_id="event-011",
            native=native,
            ledgers=ledgers,
            article_input=pack,
            report=VerificationReport(ok=True, supported=1),
        )
        from newsagent_v2.article.expand import expand_provider_article

        expand_provider_article(article, pack)
        issues = check_schema(article, pack)
        invalid = [i for i in issues if i.get("code") == "invalid_evidence_url"]
        self.assertEqual(invalid, [])
        for claim in article.get("claims") or []:
            refs = claim.get("evidence_refs") or []
            self.assertTrue(refs)
            self.assertTrue(str(refs[0].get("url")).startswith("https://"))

    def test_invalid_evidence_url_not_silently_ignored(self) -> None:
        bad_pack = {
            "event_id": "event-011",
            "evidence": [
                {
                    "id": "x",
                    "url": "factbank://event-011/P01",
                    "source_name": "FactBank",
                    "extracted_text": "x",
                }
            ],
        }
        article = {
            "schema_version": "article-output-v1",
            "event_id": "event-011",
            "headline": "H",
            "dek": "D",
            "article_body": "Body text for schema.",
            "category": "other",
            "seo_title": "H",
            "meta_description": "D",
            "slug": "h",
            "entities": [],
            "keywords": [],
            "claims": [
                {
                    "claim_id": "P01",
                    "text": "Body text for schema.",
                    "claim_type": "fact",
                    "evidence_refs": [{"url": "factbank://event-011/P01", "source": "FactBank"}],
                }
            ],
            "quotes": [],
            "article_sections": [],
            "paragraph_maps": [],
            "evidence_used": [],
        }
        issues = check_schema(article, bad_pack)
        self.assertTrue(any(i.get("code") == "invalid_evidence_url" for i in issues))

    def test_event030_unchanged(self) -> None:
        check = verify_event_030_untouched()
        self.assertTrue(check.get("ok"))
        if check.get("present"):
            self.assertEqual(check.get("expected"), PROTECTED_HASH)
            self.assertEqual(check.get("body_hash"), PROTECTED_HASH)

    def test_grounding_quote_security_regression_smoke(self) -> None:
        packet, ledgers = _packet_and_ledgers()
        native = V4NativeArticle(
            headline="Deutsche Bank awaits approval",
            dek="Deutsche Bank is awaiting final regulatory approval for custody.",
            article_body=(
                "Deutsche Bank is awaiting final regulatory approval for custody. "
                "The bank plans to launch institutional custody solutions for Bitcoin. "
                "Sabih Behzad revealed that Deutsche Bank was considering stablecoins."
            ),
            seo_title="Deutsche Bank awaits approval",
            meta_description="Deutsche Bank is awaiting final regulatory approval for custody.",
            slug="deutsche-bank-awaits-approval",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertEqual(report.unsupported, 0)
        self.assertEqual(report.ambiguous, 0)
        self.assertTrue(report.ok)
        # Security / quote path: no fabricated quotes in body.
        self.assertNotIn('"', native.article_body)


if __name__ == "__main__":
    unittest.main()


