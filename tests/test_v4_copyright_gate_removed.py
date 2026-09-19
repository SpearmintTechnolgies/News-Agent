"""Offline: V4 copyright gate removed from publishability path. No live /make."""

from __future__ import annotations

import inspect
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.grounding import check_grounding
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL
from newsagent_v2.article.qa.safety import check_safety
from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV
from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.writer.v4.evidence_depth import (
    ABSOLUTE_PUBLICATION_MINIMUM,
    ARTICLE_FULL,
    ARTICLE_LIMITED,
    ARTICLE_STANDARD,
    CAPACITY_LIMITED,
    CAPACITY_MEDIUM,
    CAPACITY_RICH,
    FULL_RANGE,
    LIMITED_RANGE,
    STANDARD_RANGE,
    DepthDecision,
    check_v4_article_depth,
)
from newsagent_v2.article.writer.v4.packet import AuthorizedFact, WriterEvidencePacket
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_KIMI,
)
from newsagent_v2.article.writer.v4.repair import run_targeted_repairs
from newsagent_v2.article.writer.v4.writer import (
    V4_KIMI_MODEL,
    ScriptedV4Writer,
    V4NativeArticle,
    _generation_packet_dict,
    build_v4_writer,
    build_v4_writer_messages,
)
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim


def _depth(
    *,
    capacity: str,
    article_type: str,
    lo: int,
    hi: int,
) -> DepthDecision:
    return DepthDecision(
        evidence_capacity=capacity,
        article_type=article_type,
        recommended_word_min=lo,
        recommended_word_max=hi,
        prefer_min=lo,
        prefer_max=hi,
        unique_propositions=12 if capacity == CAPACITY_RICH else 5,
        independent_sources=2 if capacity == CAPACITY_RICH else 1,
        primary_sources=1,
        numeric_fact_count=2,
        attribution_count=1,
        evidence_limited=capacity == CAPACITY_LIMITED,
        qa_article_mode="normal" if article_type == ARTICLE_FULL else "brief",
        reason="test",
        research={},
    )


def _body(n: int) -> str:
    # ~5 words per clause
    unit = "Markets moved after the Senate vote today. "
    text = (unit * ((n // 5) + 3)).strip()
    words = text.split()
    return " ".join(words[:n])


class TestV4CopyrightGateRemoved(unittest.TestCase):
    def test_copyright_not_in_publishability_path(self) -> None:
        from tests.fixtures.article_qa import article_input, clean_article, copied_source_paragraph

        # Copied-source fixture is copyright-critical when similarity runs.
        baseline = run_article_qa(copied_source_paragraph(), article_input())
        baseline_codes = {i.get("code") for i in baseline.get("critical_failures") or []}
        self.assertTrue(
            {"exact_phrase_overlap", "high_sentence_similarity"} & baseline_codes
        )

        skipped = run_article_qa(
            copied_source_paragraph(),
            article_input(),
            skip_copyright_similarity=True,
        )
        codes = {i.get("code") for i in skipped.get("critical_failures") or []}
        self.assertNotIn("exact_phrase_overlap", codes)
        self.assertNotIn("high_sentence_similarity", codes)
        self.assertTrue(skipped.get("metrics", {}).get("copyright_similarity_skipped"))

        # Clean article remains unaffected.
        clean = run_article_qa(clean_article(), article_input(), skip_copyright_similarity=True)
        self.assertTrue(clean.get("metrics", {}).get("copyright_similarity_skipped"))
        clean_codes = {i.get("code") for i in clean.get("critical_failures") or []}
        self.assertNotIn("exact_phrase_overlap", clean_codes)

    def test_copyright_recovery_not_called(self) -> None:
        src = inspect.getsource(run_targeted_repairs)
        self.assertNotIn("recover_copyright", src)
        import newsagent_v2.article.writer.v4.compile as compile_mod

        self.assertNotIn("recover_copyright", inspect.getsource(compile_mod.compile_v4_article))

    def test_copyright_provider_calls_zero(self) -> None:
        packet = WriterEvidencePacket(
            event_id="e",
            story_topic="t",
            authorized_facts=[
                AuthorizedFact(id="F1", proposition="The Senate blocked the crypto bill.")
            ],
            authorized_quotes=[],
            authorized_entities=["Senate"],
            forbidden=[],
        )
        ledgers = EvidenceLedgers(
            event_id="e",
            claims=(
                LedgerClaim(
                    claim_id="C1",
                    text="The Senate blocked the crypto bill.",
                    claim_type="fact",
                    evidence_ids=("e1",),
                ),
            ),
            quotes=(),
        )
        native = V4NativeArticle(
            headline="Senate blocks crypto bill after late session",
            dek="Lawmakers halted the market structure package.",
            article_body=_body(200),
            seo_title="Senate blocks crypto bill",
            meta_description="Lawmakers halted the market structure package after a late session vote.",
            slug="senate-blocks-crypto-bill",
        )
        writer = MagicMock()
        writer.model = V4_KIMI_MODEL
        writer.environ = {ENV_ALLOW_KIMI: "true"}
        with patch(
            "newsagent_v2.article.writer.v4.copyright_recovery.recover_copyright"
        ) as mocked:
            repaired, _report, log = run_targeted_repairs(
                native,
                packet=packet,
                ledgers=ledgers,
                article_input={"event_id": "e", "evidence": []},
                writer=writer,
            )
            mocked.assert_not_called()
        self.assertFalse(log.copyright_rejected)
        self.assertEqual(log.copyright_recovery.get("active"), False)

    def test_raw_source_not_writer_input(self) -> None:
        packet = WriterEvidencePacket(
            event_id="e",
            story_topic="t",
            authorized_facts=[
                AuthorizedFact(id="F1", proposition="Bitcoin ETFs recorded net outflows.")
            ],
            authorized_quotes=[],
            authorized_entities=["Bitcoin"],
            forbidden=[],
            source_context={
                "source_names": ["CoinDesk"],
                "bodies": ["RAW SOURCE ARTICLE PROSE MUST NOT REACH THE WRITER"],
            },
        )
        gen = _generation_packet_dict(packet)
        blob = str(gen)
        self.assertNotIn("RAW SOURCE", blob)
        self.assertNotIn("bodies", gen.get("source_context") or {})
        messages = build_v4_writer_messages(packet)
        self.assertNotIn("RAW SOURCE", str(messages))

    def test_factbank_semantic_writer_boundary(self) -> None:
        packet = WriterEvidencePacket(
            event_id="e",
            story_topic="t",
            authorized_facts=[
                AuthorizedFact(
                    id="F1",
                    proposition="Deutsche Bank awaits regulatory approval for crypto custody.",
                    entities=("Deutsche Bank",),
                )
            ],
            authorized_quotes=[],
            authorized_entities=["Deutsche Bank"],
            forbidden=["invent motives"],
        )
        gen = _generation_packet_dict(packet)
        self.assertIn("authorized_facts", gen)
        self.assertEqual(gen["authorized_facts"][0]["proposition"].startswith("Deutsche"), True)
        self.assertNotIn("extracted_text", gen)

    def test_quote_grounding_still_active(self) -> None:
        from newsagent_v2.article.qa.claims import check_claims

        article = {
            "headline": "Official comments on ETF flows",
            "dek": "A spokesperson spoke after the vote.",
            "article_body": 'The spokesperson said, "We invented this quote entirely."',
            "seo_title": "Official comments on ETF flows",
            "meta_description": "A spokesperson spoke after the vote on bitcoin ETF flows.",
            "slug": "official-comments-etf-flows",
            "category": "markets",
            "claims": [],
            "quotes": [
                {
                    "text": "We invented this quote entirely.",
                    "kind": "direct",
                    "attribution": "spokesperson",
                    # missing evidence_refs â†’ critical
                }
            ],
        }
        pack = {"event_id": "e", "evidence": [{"extracted_text": "No matching quote here."}]}
        issues, _ = check_claims(article, pack)
        self.assertTrue(
            any(
                i.get("severity") == SEVERITY_CRITICAL
                and str(i.get("code") or "")
                in {"direct_quote_no_source", "quote_missing_evidence", "direct_quote_no_attribution"}
                for i in issues
            )
        )

    def test_unsupported_assertion_and_grounding_still_critical(self) -> None:
        from newsagent_v2.article.writer.v4.verify import (
            STATUS_UNSUPPORTED,
            verify_v4_native,
        )

        packet = WriterEvidencePacket(
            event_id="e",
            story_topic="t",
            authorized_facts=[
                AuthorizedFact(id="F1", proposition="The Senate blocked the crypto bill.")
            ],
            authorized_quotes=[],
            authorized_entities=["Senate"],
            forbidden=[],
        )
        ledgers = EvidenceLedgers(
            event_id="e",
            claims=(
                LedgerClaim(
                    claim_id="C1",
                    text="The Senate blocked the crypto bill.",
                    claim_type="fact",
                    evidence_ids=("e1",),
                ),
            ),
            quotes=(),
        )
        native = V4NativeArticle(
            headline="Senate blocks crypto bill",
            dek="Lawmakers halted the package.",
            article_body=(
                "The Senate blocked the crypto bill. "
                "Analysts predict bitcoin will double next week without evidence."
            ),
            seo_title="Senate blocks crypto bill",
            meta_description="Lawmakers halted the market structure package.",
            slug="senate-blocks-crypto-bill",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertTrue(
            report.unsupported > 0
            or any(row.status == STATUS_UNSUPPORTED for row in report.rows)
        )

    def test_security_still_critical(self) -> None:
        article = {
            "headline": "Market update after senate vote",
            "dek": "Flows reversed.",
            "article_body": _body(200) + " See C:\\Secrets\\api_key.txt for details.",
            "seo_title": "Market update after senate vote",
            "meta_description": "Flows reversed after the senate vote on crypto legislation.",
            "slug": "market-update-senate-vote",
            "category": "markets",
            "claims": [],
        }
        issues, _ = check_safety(article, {"event_id": "e", "evidence": []})
        self.assertTrue(any(i.get("severity") == SEVERITY_CRITICAL for i in issues))

    def test_depth_bands(self) -> None:
        self.assertEqual(FULL_RANGE, (250, 400))
        self.assertEqual(STANDARD_RANGE, (150, 249))
        self.assertEqual(LIMITED_RANGE, (120, 149))
        self.assertEqual(ABSOLUTE_PUBLICATION_MINIMUM, 120)

        full = _depth(capacity=CAPACITY_RICH, article_type=ARTICLE_FULL, lo=250, hi=400)
        std = _depth(capacity=CAPACITY_MEDIUM, article_type=ARTICLE_STANDARD, lo=150, hi=249)
        lim = _depth(capacity=CAPACITY_LIMITED, article_type=ARTICLE_LIMITED, lo=120, hi=149)

        self.assertTrue(check_v4_article_depth({"article_body": _body(249)}, full))
        self.assertFalse(check_v4_article_depth({"article_body": _body(250)}, full))
        self.assertTrue(check_v4_article_depth({"article_body": _body(149)}, std))
        self.assertFalse(check_v4_article_depth({"article_body": _body(150)}, std))
        self.assertTrue(check_v4_article_depth({"article_body": _body(119)}, lim))
        self.assertFalse(check_v4_article_depth({"article_body": _body(120)}, lim))
        rich_issues = check_v4_article_depth({"article_body": _body(70)}, full)
        self.assertTrue(rich_issues)
        self.assertTrue(
            any(
                i.get("code") in {"below_absolute_publication_minimum", "rich_capacity_underproduced", "below_article_type_minimum"}
                for i in rich_issues
            )
        )

    def test_kimi_primary_writer_groq_not_used(self) -> None:
        env = {
            ENV_ALLOW_KIMI: "true",
            ENV_PROVIDER: PROVIDER_KIMI,
            ENV_MODEL: V4_KIMI_MODEL,
            ENV_MAX_PROVIDER_ATTEMPTS: "1",
            ENV_ALLOW_PAID_QWEN: "0",
            KIMI_KEY_ENV: "test-key",
        }
        writer = build_v4_writer(environ=env, enable_failover=True)
        self.assertEqual(writer.provider, PROVIDER_KIMI)
        self.assertEqual(writer.model, V4_KIMI_MODEL)
        self.assertFalse(hasattr(writer, "fallback"))


if __name__ == "__main__":
    unittest.main()


