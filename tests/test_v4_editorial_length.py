"""Focused V4 editorial length / regeneration tests."""

from __future__ import annotations

import os
import unittest

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.expand import (
    BELOW_EDITORIAL_TARGET_WARNING,
    EDITORIAL_TARGET_MIN_WORDS,
    realize_editorial_length,
)
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    FORBIDDEN_WRITERS,
    ScriptedV4Writer,
    V4NativeArticle,
    assert_v4_writer_is_free,
    build_v4_writer_messages,
)
from newsagent_v2.article.writer.v4.packet import build_writer_evidence_packet


def _ledgers() -> EvidenceLedgers:
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
        LedgerClaim(
            "C04",
            "Investigators confirmed identity documents and payment history were involved.",
            "fact",
            ("e1",),
        ),
        LedgerClaim(
            "C05",
            "Northwind limited the incident to retail payments customer files.",
            "fact",
            ("e1",),
        ),
        LedgerClaim(
            "C06",
            "The company opened an internal review of email authentication controls.",
            "fact",
            ("e1",),
        ),
        LedgerClaim(
            "C07",
            "Customer support teams prepared guidance for callers asking about the exposure.",
            "fact",
            ("e1",),
        ),
        LedgerClaim(
            "C08",
            "Northwind said the spoofed message used a lookalike government domain string.",
            "fact",
            ("e1",),
        ),
    )
    return EvidenceLedgers(event_id="event-exp", claims=claims, quotes=())


def _packet():
    return build_writer_evidence_packet(
        event_id="event-exp",
        ledgers=_ledgers(),
        story_topic="Northwind exposure",
    )


def _pad_authorized(body: str, min_words: int) -> str:
    filler = (
        "Northwind Payments disclosed that 12400 customer records were exposed. "
        "A spoofed government-domain email reached company staff. "
        "Security staff began notifying affected users after the disclosure."
    )
    while word_count(body) < min_words:
        body = (body + " " + filler).strip()
    return body


def _full_regen_body(min_words: int = 280) -> str:
    return _pad_authorized(
        "Northwind Payments disclosed that 12400 customer records were exposed. "
        "A spoofed government-domain email reached company staff. "
        "Security staff began notifying affected users after the disclosure. "
        "Investigators confirmed identity documents and payment history were involved. "
        "Northwind limited the incident to retail payments customer files. "
        "The company opened an internal review of email authentication controls. "
        "Customer support teams prepared guidance for callers asking about the exposure. "
        "Northwind said the spoofed message used a lookalike government domain string.",
        min_words,
    )


def _full_regen_payload(native: V4NativeArticle, min_words: int = 280) -> dict:
    return {
        "headline": "Northwind Payments disclosed that 12400 customer records were exposed",
        "dek": "A spoofed government-domain email reached company staff.",
        "article_body": _full_regen_body(min_words),
        "seo_title": native.seo_title,
        "meta_description": native.meta_description,
        "slug": native.slug,
    }


def _native_words(approx: int, *, include_late_facts: bool = False) -> V4NativeArticle:
    if approx >= 250:
        body = _pad_authorized(
            "Northwind Payments disclosed that 12400 customer records were exposed. "
            "A spoofed government-domain email reached company staff. "
            "Security staff began notifying affected users after the disclosure. "
            "Investigators confirmed identity documents and payment history were involved. "
            "Northwind limited the incident to retail payments customer files. "
            "The company opened an internal review of email authentication controls.",
            approx,
        )
    else:
        body = (
            "Northwind Payments disclosed that 12400 customer records were exposed. "
            "A spoofed government-domain email reached company staff. "
            "Security staff began notifying affected users after the disclosure."
        )
        body = _pad_authorized(body, approx)
        if include_late_facts:
            body += (
                " Investigators confirmed identity documents and payment history were involved."
            )
    return V4NativeArticle(
        headline="Northwind Payments discloses customer-record exposure",
        dek="A spoofed government-domain email reached company staff.",
        article_body=body,
        seo_title="Northwind customer records exposed",
        meta_description="Northwind Payments said 12400 customer records were exposed.",
        slug="northwind-customer-records-exposed",
    )


class V4EditorialLengthTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        os.environ["NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS"] = "120"

    def test_a_no_expansion_when_already_280(self) -> None:
        native = _native_words(280)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(native.as_dict(), expansion_text="SHOULD NOT RUN")
        out, _report2, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertFalse(expansion.triggered)
        self.assertEqual(writer.expansion_calls, 0)
        self.assertGreaterEqual(out.word_count, EDITORIAL_TARGET_MIN_WORDS)
        self.assertTrue(expansion.editorial_target_met)

    def test_b_underproduction_triggers_fresh_regeneration(self) -> None:
        native = _native_words(170)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(
            native.as_dict(),
            regeneration_payload=_full_regen_payload(native),
            expansion_text="LEGACY MUST NOT RUN",
        )
        out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertTrue(expansion.triggered)
        self.assertEqual(writer.expansion_calls, 0)
        self.assertEqual(writer.regeneration_calls, 1)
        self.assertGreaterEqual(out.word_count, EDITORIAL_TARGET_MIN_WORDS)

    def test_c_validated_regeneration_reaches_target(self) -> None:
        native = _native_words(170)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(
            native.as_dict(),
            regeneration_payload=_full_regen_payload(native),
        )
        out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertGreaterEqual(out.word_count, EDITORIAL_TARGET_MIN_WORDS)
        self.assertTrue(expansion.editorial_target_met)
        self.assertGreater(expansion.validated_words, 0)

    def test_d_regeneration_still_under_rejects(self) -> None:
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
        ledgers = EvidenceLedgers(event_id="event-exp", claims=claims, quotes=())
        packet = build_writer_evidence_packet(
            event_id="event-exp", ledgers=ledgers, story_topic="Northwind"
        )
        body = _pad_authorized(
            "Northwind Payments disclosed that 12400 customer records were exposed. "
            "A spoofed government-domain email reached company staff. "
            "Security staff began notifying affected users after the disclosure.",
            170,
        )
        native = V4NativeArticle(
            headline="Northwind exposure",
            dek="Staff email spoof.",
            article_body=body,
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(
            native.as_dict(),
            regeneration_payload=native.as_dict(),
            expansion_text="Aliens bought bitcoin overnight.",
        )
        _out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertTrue(expansion.triggered)
        self.assertEqual(writer.expansion_calls, 0)
        self.assertTrue(expansion.rejected)
        self.assertIn("WRITER_UNDERPRODUCED", str(expansion.reject_reason))
        self.assertFalse(expansion.editorial_target_met)
        self.assertEqual(expansion.warning, BELOW_EDITORIAL_TARGET_WARNING)

    def test_e_unsupported_regeneration_rejected(self) -> None:
        native = _native_words(170)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        bad = {
            **native.as_dict(),
            "article_body": _pad_authorized(
                "Regulators in three countries opened a criminal inquiry on Tuesday. "
                "Northwind Payments disclosed that 12400 customer records were exposed.",
                260,
            ),
        }
        writer = ScriptedV4Writer(native.as_dict(), regeneration_payload=bad)
        out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertTrue(expansion.attempted)
        self.assertTrue(expansion.rejected)
        self.assertNotIn("criminal inquiry", out.article_body)

    def test_f_unauthorized_number_in_regeneration_rejected(self) -> None:
        native = _native_words(170)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        bad = {
            **native.as_dict(),
            "article_body": _pad_authorized(
                "Investigators confirmed that 437 million accounts were involved. "
                "Northwind Payments disclosed that 12400 customer records were exposed.",
                260,
            ),
        }
        writer = ScriptedV4Writer(native.as_dict(), regeneration_payload=bad)
        out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertTrue(expansion.rejected or "437" not in out.article_body)

    def test_g_copyright_trap_not_via_legacy_expand(self) -> None:
        native = _native_words(170)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        source = (
            "Investigators confirmed identity documents and payment history were involved "
            "in a uniquely phrased copyright trap sentence about Northwind retail files today."
        )
        writer = ScriptedV4Writer(
            native.as_dict(),
            regeneration_payload=_full_regen_payload(native),
            expansion_text=source,
        )
        out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={
                "evidence": [
                    {
                        "source": "Wire",
                        "title": "Northwind",
                        "summary": source,
                        "url": "https://example.com/n",
                    }
                ]
            },
            report=report,
            writer=writer,
        )
        self.assertEqual(writer.expansion_calls, 0)
        self.assertNotIn("uniquely phrased copyright trap", out.article_body)
        self.assertTrue(expansion.editorial_target_met or expansion.validated_words >= 0)

    def test_h_short_regeneration_rejected_no_third_pass(self) -> None:
        native = _native_words(170)
        packet = _packet()
        ledgers = _ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(
            native.as_dict(),
            regeneration_payload=native.as_dict(),
        )
        _out, _r, expansion = realize_editorial_length(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
        )
        self.assertEqual(writer.regeneration_calls, 1)
        self.assertTrue(expansion.rejected)
        self.assertIn("WRITER_UNDERPRODUCED", str(expansion.reject_reason))

    def test_i_kimi_forbidden(self) -> None:
        self.assertTrue(any("kimi" in item for item in FORBIDDEN_WRITERS))
        with self.assertRaises(RuntimeError):
            assert_v4_writer_is_free("moonshotai.kimi-k2.5", environ={"NEWSAGENT_V2_V4_ALLOW_KIMI": "0"})
        prompt = build_v4_writer_messages(_packet())[0]["content"]
        self.assertIn("250", prompt)
        self.assertIn("400", prompt)
        self.assertIn("complete professional news article", prompt)
        self.assertIn("not a summary or brief", prompt)
        self.assertIn("approximately 300", prompt)


if __name__ == "__main__":
    unittest.main()


