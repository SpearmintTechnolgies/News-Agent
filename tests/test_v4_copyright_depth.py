"""Copyright rewrite must preserve depth; deletion is last resort."""

from __future__ import annotations

import unittest

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.packet import build_writer_evidence_packet
from newsagent_v2.article.writer.v4.repair import (
    REPAIR_LENGTH_RETENTION_ADVISORY,
    independent_semantic_rewrite,
    repair_copyright_sentences,
)
from newsagent_v2.article.writer.v4.verify import STATUS_SUPPORTED, verify_v4_native
from newsagent_v2.article.writer.v4.writer import V4NativeArticle


def _packet():
    claims = (
        LedgerClaim(
            "C01",
            "Northwind Payments disclosed that 12400 customer records were exposed.",
            "fact",
            ("e1",),
        ),
        LedgerClaim(
            "C02",
            "A spoofed government-domain email reached company staff.",
            "fact",
            ("e1",),
        ),
        LedgerClaim(
            "C03",
            "Security staff began notifying affected users after the disclosure.",
            "fact",
            ("e1",),
        ),
    )
    ledgers = EvidenceLedgers(event_id="event-depth", claims=claims, quotes=())
    return build_writer_evidence_packet(
        event_id="event-depth", ledgers=ledgers, story_topic="Northwind"
    ), ledgers


class CopyrightDepthPreservationTests(unittest.TestCase):
    def test_rewrite_preferred_over_drop(self) -> None:
        packet, _ledgers = _packet()
        offender = (
            "Northwind Payments disclosed that 12400 customer records were exposed "
            "after a carefully copied source-shaped clause about retail payments files."
        )
        native = V4NativeArticle(
            headline="Northwind disclosure",
            dek="A spoofed government-domain email reached company staff.",
            article_body=(
                "Security staff began notifying affected users after the disclosure. " + offender
            ),
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        before = word_count(native.article_body)
        repaired, actions = repair_copyright_sentences(native, packet, [offender])
        self.assertTrue(actions)
        self.assertTrue(any(a.kind == "copyright_sentence_rewrite" for a in actions))
        self.assertFalse(any(a.kind == "copyright_sentence_drop" for a in actions))
        self.assertNotIn(offender, repaired.article_body)
        self.assertGreaterEqual(word_count(repaired.article_body), int(before * 0.6))

    def test_retention_ratio_diagnostic(self) -> None:
        packet, _ledgers = _packet()
        sentence = (
            "Northwind Payments disclosed that 12400 customer records were exposed "
            "while security staff began notifying affected users after the disclosure."
        )
        rewritten, meta = independent_semantic_rewrite(sentence, packet)
        self.assertNotEqual(rewritten, sentence)
        self.assertIn("repair_length_retention_ratio", meta)
        self.assertGreaterEqual(
            meta["repair_length_retention_ratio"], REPAIR_LENGTH_RETENTION_ADVISORY
        )
        self.assertGreaterEqual(meta["replacement_words"], 8)

    def test_structural_realization_not_synonym_swap(self) -> None:
        packet, _ledgers = _packet()
        sentence = "Northwind Payments disclosed that 12400 customer records were exposed."
        rewritten, meta = independent_semantic_rewrite(sentence, packet)
        self.assertNotEqual(rewritten.lower(), sentence.lower())
        self.assertEqual(meta.get("realization"), "independent_structural")
        self.assertNotEqual(meta.get("strategy"), "frame_0")
        # Must keep locked facts.
        self.assertIn("12400", rewritten)
        self.assertIn("Northwind", rewritten)
        # Passiveive / restructure should not be a near-copy.
        from difflib import SequenceMatcher

        self.assertLess(SequenceMatcher(None, rewritten.lower(), sentence.lower()).ratio(), 0.92)
        # Bounded alternates must differ across attempts when multiple exist.
        attempts = []
        from newsagent_v2.article.writer.v4.repair import independent_semantic_rewrite_attempts

        attempts = independent_semantic_rewrite_attempts(sentence, packet, max_attempts=3)
        self.assertGreaterEqual(len(attempts), 1)
        self.assertTrue(all(text != sentence for text, _ in attempts))

    def test_editorial_frame_supported_by_verifier(self) -> None:
        packet, ledgers = _packet()
        native = V4NativeArticle(
            headline="Northwind Payments discloses customer-record exposure",
            dek="A spoofed government-domain email reached company staff.",
            article_body=(
                "According to the disclosed information, Northwind Payments disclosed that "
                "12400 customer records were exposed. "
                "Security staff began notifying affected users after the disclosure."
            ),
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertTrue(any(row.status == STATUS_SUPPORTED for row in report.rows))
        self.assertFalse(
            any(row.issue_code == "unsupported_inference" for row in report.rows)
        )


if __name__ == "__main__":
    unittest.main()


