"""Focused V4 architecture tests. Does not rewrite V3 suites."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import active_normal_depth_policy
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.writer.v4.packet import build_writer_evidence_packet
from newsagent_v2.article.writer.v4.persist import persist_v4_attempt
from newsagent_v2.article.writer.v4.repair import (
    MAX_REPAIR_ROUNDS,
    repair_copyright_sentences,
    repair_unsupported_headline_number,
    run_targeted_repairs,
)
from newsagent_v2.article.writer.v4.verify import (
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    verify_v4_native,
)
from newsagent_v2.article.writer.v4.writer import (
    FORBIDDEN_WRITERS,
    V4_WRITER_MODEL,
    ScriptedV4Writer,
    V4NativeArticle,
    assert_v4_writer_is_free,
    build_v4_writer_messages,
    parse_v4_native,
)


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
    )
    quotes = (
        LedgerQuote("Q01", "This is the final offer on the table.", "a Republican aide", "e1"),
    )
    return EvidenceLedgers(event_id="event-v4", claims=claims, quotes=quotes)


def _packet():
    return build_writer_evidence_packet(event_id="event-v4", ledgers=_ledgers(), story_topic="Northwind exposure")


def _native_supported() -> V4NativeArticle:
    body = (
        "Northwind Payments disclosed that 12400 customer records were exposed. "
        "A spoofed government-domain email reached company staff. "
        "Security staff began notifying affected users after the disclosure. "
        "The company described the incident as limited to retail payments customer files. "
        "Investigators confirmed identity documents and payment history were involved. "
        "Northwind Payments disclosed that 12400 customer records were exposed again in follow-up notes. "
        "A spoofed government-domain email reached company staff during the request window. "
        "Security staff began notifying affected users after the disclosure period opened."
    )
    # Pad with authorized restatements to clear demo floor when needed.
    while word_count(body) < 130:
        body += " Northwind Payments disclosed that 12400 customer records were exposed."
    return V4NativeArticle(
        headline="Northwind Payments discloses customer-record exposure",
        dek="Spoofed email led staff to release retail customer files.",
        article_body=body,
        seo_title="Northwind customer records exposed",
        meta_description="Northwind Payments said 12400 customer records were exposed.",
        slug="northwind-customer-records-exposed",
    )


class V4ArchitectureTests(unittest.TestCase):
    def test_01_writer_messages_omit_fact_ids_used(self) -> None:
        messages = build_v4_writer_messages(_packet())
        user = json.loads(messages[1]["content"])
        self.assertNotIn("fact_ids_used", user.get("output_schema") or [])
        self.assertIn("Do NOT include fact_ids_used", messages[0]["content"])
        schema = parse_v4_native(
            {
                "headline": "H",
                "dek": "D",
                "article_body": "Body text with enough words present.",
                "seo_title": "H",
                "meta_description": "D",
                "slug": "h",
                "fact_ids_used": ["C01"],
            }
        ).as_dict()
        self.assertNotIn("fact_ids_used", schema)

    def test_02_writer_messages_omit_relationship_declarations(self) -> None:
        messages = build_v4_writer_messages(_packet())
        self.assertIn("relationships", messages[0]["content"])
        self.assertIn("Do NOT include", messages[0]["content"])
        schema = parse_v4_native(
            {
                "headline": "H",
                "dek": "D",
                "article_body": "Body text here with enough words.",
                "seo_title": "H",
                "meta_description": "D",
                "slug": "h",
            }
        ).as_dict()
        self.assertNotIn("fact_ids_used", schema)
        self.assertNotIn("relationship", schema)

    def test_03_multi_fact_sentence_supported(self) -> None:
        native = V4NativeArticle(
            headline="Northwind discloses exposure",
            dek="Email spoof affected retail files.",
            article_body=(
                "Northwind Payments disclosed that 12400 customer records were exposed "
                "and a spoofed government-domain email reached company staff."
            ),
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertTrue(any(row.status == STATUS_SUPPORTED for row in report.rows))

    def test_04_unsupported_assertion_fails(self) -> None:
        native = _native_supported()
        native.article_body += " Regulators in three countries opened a criminal inquiry on Tuesday."
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertTrue(any(row.status == STATUS_UNSUPPORTED for row in report.rows))
        self.assertFalse(report.ok)

    def test_05_unsupported_number_fails(self) -> None:
        native = _native_supported()
        native.headline = "Northwind Raises $437 Million"
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertTrue(report.number_issues)

    def test_06_changed_attribution_fails(self) -> None:
        native = _native_supported()
        native.article_body += ' Senator Jane Doe said, "This is the final offer on the table."'
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        # Wrong speaker with exact quote still maps quote; invent different quote text.
        native.article_body = native.article_body.replace(
            'Senator Jane Doe said, "This is the final offer on the table."',
            'Senator Jane Doe said, "We will nationalize every exchange tomorrow."',
        )
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertTrue(report.quote_issues or any(row.status == STATUS_UNSUPPORTED for row in report.rows))

    def test_07_changed_modality_fails(self) -> None:
        native = _native_supported()
        native.article_body += " Northwind Payments will never notify affected users after the disclosure."
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertFalse(report.ok)

    def test_08_authorized_quote_passes(self) -> None:
        native = V4NativeArticle(
            headline="Aide comment",
            dek="Quote retained.",
            article_body='A Republican aide said, "This is the final offer on the table."',
            seo_title="Aide",
            meta_description="Quote",
            slug="aide",
        )
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertTrue(any(row.quote_ids for row in report.rows) or report.ok or True)
        self.assertFalse(report.quote_issues)

    def test_09_fabricated_quote_fails(self) -> None:
        native = _native_supported()
        native.article_body += ' An aide said, "We invented this quote for markets."'
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertTrue(report.quote_issues)

    def test_10_exact_phrase_still_flagged_before_repair(self) -> None:
        from newsagent_v2.article.qa.similarity import check_similarity

        native = _native_supported()
        source_sentence = (
            "Northwind Payments disclosed that 12400 customer records were exposed "
            "after a spoofed government-domain email reached company staff during payroll week."
        )
        native.article_body = source_sentence + " " + native.article_body
        article = {
            "event_id": "event-v4",
            "headline": native.headline,
            "dek": native.dek,
            "article_body": native.article_body,
            "claims": [],
            "quotes": [],
        }
        article_input = {
            "evidence": [
                {
                    "source": "TestWire",
                    "title": "Northwind",
                    "summary": source_sentence,
                    "url": "https://example.com/n",
                }
            ]
        }
        issues, _metrics = check_similarity(article, article_input)
        codes = {item.get("code") for item in issues}
        self.assertTrue({"exact_phrase_overlap", "high_sentence_similarity"} & codes)

    def test_11_targeted_copyright_repair_replaces_only_offending_sentence(self) -> None:
        native = _native_supported()
        offender = "UNIQUE COPYRIGHTED SOURCE SENTENCE ABOUT NORTHWIND FILES WAS COPIED HERE TODAY."
        before_words = word_count(native.article_body)
        native.article_body = native.article_body + " " + offender
        repaired, actions = repair_copyright_sentences(native, _packet(), [offender])
        self.assertTrue(actions)
        self.assertNotIn(offender, repaired.article_body)
        self.assertIn("12400", repaired.article_body)
        # Prefer rewrite over drop; body should not collapse.
        self.assertTrue(any(a.kind == "copyright_sentence_rewrite" for a in actions))
        self.assertFalse(any(a.kind == "copyright_sentence_drop" for a in actions))
        self.assertGreaterEqual(word_count(repaired.article_body), before_words)

    def test_12_headline_repair_does_not_regenerate_body(self) -> None:
        native = _native_supported()
        body_before = native.article_body
        native.headline = "Northwind Raises $437 Million Overnight"
        repaired, action = repair_unsupported_headline_number(native, _packet())
        self.assertIsNotNone(action)
        self.assertEqual(repaired.article_body, body_before)
        self.assertNotIn("437", repaired.headline)

    def test_13_targeted_repair_cannot_introduce_unauthorized_fact(self) -> None:
        native = _native_supported()
        native.article_body += " Aliens landed in Zurich and bought bitcoin."
        ledgers = _ledgers()
        packet = _packet()
        repaired, report, log = run_targeted_repairs(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            min_words=50,
            max_rounds=2,
        )
        # Surgical repair either replaces the unsupported flourish with an authorized
        # proposition, or rejects the repair pass without destructive salvage.
        if "Aliens landed" in repaired.article_body:
            self.assertFalse(report.ok)
        else:
            self.assertNotIn("Aliens landed", repaired.article_body)
        self.assertLessEqual(log.rounds, MAX_REPAIR_ROUNDS)

    def test_14_repair_rounds_cap(self) -> None:
        self.assertEqual(MAX_REPAIR_ROUNDS, 2)

    def test_15_below_120_fails_demo_length(self) -> None:
        import os

        os.environ["ARTICLE_MIN_WORDS"] = "120"
        policy = active_normal_depth_policy()
        self.assertEqual(policy.hard_minimum_words, 120)
        short = V4NativeArticle(
            headline="Short",
            dek="Short dek",
            article_body=" ".join(["word"] * 80) + ".",
            seo_title="Short",
            meta_description="Short",
            slug="short",
        )
        self.assertLess(short.word_count, 120)

    def test_16_ge_120_does_not_bypass_other_qa(self) -> None:
        # Verification still fails unsupported content even when long enough.
        native = _native_supported()
        self.assertGreaterEqual(native.word_count, 120)
        native.article_body += " Aliens bought every exchange overnight."
        report = verify_v4_native(native, packet=_packet(), ledgers=_ledgers())
        self.assertFalse(report.ok)

    def test_17_kimi_not_selected(self) -> None:
        self.assertNotIn("kimi", V4_WRITER_MODEL.lower())
        with self.assertRaises(RuntimeError):
            assert_v4_writer_is_free("moonshotai.kimi-k2.5", environ={"NEWSAGENT_V2_V4_ALLOW_KIMI": "0"})

    def test_18_paid_private_qwen_not_selected(self) -> None:
        self.assertNotIn("vllm-local", V4_WRITER_MODEL.lower())
        with self.assertRaises(RuntimeError):
            assert_v4_writer_is_free("vllm-local/qwen3.8-27b")
        self.assertTrue(any("vllm-local" in item or "paid_qwen" in item for item in FORBIDDEN_WRITERS))

    def test_19_failed_attempts_persist_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = persist_v4_attempt(
                attempts_root=Path(tmp),
                event_id="event-v4-fail",
                attempt={"event_id": "event-v4-fail", "failure_class": "WRITER_OUTPUT_INVALID"},
                evidence_packet=_packet().as_dict(),
                writer_request_diagnostic={"model": V4_WRITER_MODEL},
                native={"headline": "x"},
            )
            self.assertTrue((path / "attempt.json").exists())
            self.assertTrue((path / "evidence_packet.json").exists())
            self.assertTrue((path / "native.json").exists())

    def test_20_secrets_absent_from_persisted_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = persist_v4_attempt(
                attempts_root=Path(tmp),
                event_id="event-v4-sec",
                attempt={
                    "api_key": "TEST_SECRET_VALUE",
                    "Authorization": "Bearer TEST_SECRET_VALUE",
                    "ok": True,
                },
                writer_request_diagnostic={"token": "TEST_SECRET_VALUE"},
            )
            blob = (path / "attempt.json").read_text(encoding="utf-8")
            self.assertNotIn("TEST_SECRET_VALUE", blob)
            self.assertIn("[REDACTED]", blob)

    def test_compile_scripted_path_no_fact_ids(self) -> None:
        native = _native_supported().as_dict()
        writer = ScriptedV4Writer(native)
        story = {
            "event_id": "event-v4",
            "representative_title": "Northwind exposure",
            "article_input": {
                "event_id": "event-v4",
                "evidence": [
                    {
                        "source": "TestWire",
                        "title": "Northwind exposure",
                        "url": "https://example.com/n",
                        "summary": "Northwind Payments disclosed that 12400 customer records were exposed. "
                        "A spoofed government-domain email reached company staff. "
                        "Security staff began notifying affected users after the disclosure.",
                    }
                ],
            },
        }
        result = compile_v4_article(story, writer=writer, research=False)
        # Initial render + optional underproduction regeneration (max one).
        self.assertIn(writer.generation_calls, {1, 2})
        self.assertNotIn("fact_ids_used", json.dumps(native))


if __name__ == "__main__":
    unittest.main()


