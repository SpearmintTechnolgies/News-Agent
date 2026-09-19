"""Offline validation for V4 Kimi prose path + relaxed non-safety QA.

No live provider calls. No /make.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from newsagent_v2.article.qa.mechanics import check_mechanics
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, SEVERITY_WARNING, build_qa_result
from newsagent_v2.article.qa.seo import check_seo
from newsagent_v2.article.qa.similarity import check_similarity
from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV
from newsagent_v2.article.writer.v4.evidence_depth import assess_evidence_capacity
from newsagent_v2.article.writer.v4.factbank import FactBank, FactProposition
from newsagent_v2.article.writer.v4.packet import AuthorizedFact, WriterEvidencePacket
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_KIMI,
    KimiChatTransport,
    build_transport,
    resolve_v4_provider_specs,
)
from newsagent_v2.article.writer.v4.writer import (
    V4_KIMI_MODEL,
    assert_v4_writer_is_free,
    build_v4_writer,
    build_v4_writer_messages,
    _generation_packet_dict,
)


def _packet() -> WriterEvidencePacket:
    return WriterEvidencePacket(
        event_id="event-test",
        story_topic="Deutsche Bank crypto custody",
        authorized_facts=[
            AuthorizedFact(
                id="F1",
                proposition="Deutsche Bank is awaiting regulatory approval for crypto custody.",
                attribution="bank",
                numbers=(),
                entities=("Deutsche Bank",),
            ),
            AuthorizedFact(
                id="F2",
                proposition="Initial supported assets include Bitcoin and Ether.",
                numbers=(),
                entities=("Bitcoin", "Ether"),
            ),
        ],
        authorized_quotes=[],
        authorized_entities=["Deutsche Bank", "Bitcoin", "Ether"],
        forbidden=["invent market reaction"],
    )


class TestV4KimiProsePathOffline(unittest.TestCase):
    def test_kimi_adapter_contract_pass(self) -> None:
        env = {
            ENV_ALLOW_KIMI: "true",
            ENV_PROVIDER: PROVIDER_KIMI,
            ENV_MODEL: V4_KIMI_MODEL,
            ENV_MAX_PROVIDER_ATTEMPTS: "1",
            ENV_ALLOW_PAID_QWEN: "0",
            KIMI_KEY_ENV: "test-kimi-key-not-real",
        }
        specs = resolve_v4_provider_specs(env)
        self.assertTrue(specs["allow_kimi"])
        self.assertEqual(specs["primary"].provider, PROVIDER_KIMI)
        self.assertTrue(specs["primary"].allowed)
        transport = build_transport(
            provider=PROVIDER_KIMI,
            model=V4_KIMI_MODEL,
            environ=env,
            allow_kimi=True,
        )
        self.assertIsInstance(transport, KimiChatTransport)
        self.assertTrue(transport.allowed)
        self.assertEqual(transport.model, V4_KIMI_MODEL)
        assert_v4_writer_is_free(V4_KIMI_MODEL, environ=env)
        writer = build_v4_writer(environ=env, enable_failover=False)
        self.assertEqual(writer.provider, PROVIDER_KIMI)
        self.assertEqual(getattr(writer, "renderer_name", ""), "v4_natural_prose_writer")
        self.assertFalse(hasattr(writer, "fallback"))

    def test_kimi_idle_without_allow(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_v4_writer_is_free(V4_KIMI_MODEL, environ={ENV_ALLOW_KIMI: "0"})
        transport = build_transport(
            provider=PROVIDER_KIMI,
            model=V4_KIMI_MODEL,
            environ={KIMI_KEY_ENV: "x"},
            allow_kimi=False,
        )
        result = transport.complete(messages=[{"role": "user", "content": "hi"}])
        self.assertFalse(result.ok)
        self.assertIn("ALLOW_KIMI", str(result.error or ""))

    def test_kimi_semantic_only_writer_input(self) -> None:
        packet = _packet()
        messages = build_v4_writer_messages(packet)
        blob = json.dumps(messages)
        for banned in (
            "extracted_text",
            "source_article_body",
            "matched_source_fragment",
            "offending_sentence",
        ):
            self.assertNotIn(banned, blob)
        gen = _generation_packet_dict(packet)
        self.assertIn("authorized_facts", gen)
        self.assertNotIn("extracted_text", gen)

    def test_kimi_raw_source_exclusion(self) -> None:
        packet = WriterEvidencePacket(
            event_id="event-test",
            story_topic="Deutsche Bank crypto custody",
            authorized_facts=_packet().authorized_facts,
            authorized_quotes=[],
            authorized_entities=["Deutsche Bank", "Bitcoin", "Ether"],
            forbidden=["invent market reaction"],
            source_context={
                "source_names": ["Cointelegraph"],
                "publication_timestamps": ["2026-09-17"],
                "bodies": ["RAW SOURCE PROSE MUST NEVER REACH WRITER"],
            },
        )
        gen = _generation_packet_dict(packet)
        self.assertNotIn("bodies", gen.get("source_context") or {})
        self.assertNotIn("RAW SOURCE", json.dumps(gen))

    def test_kimi_copyright_repair_removed_from_active_path(self) -> None:
        import inspect

        from newsagent_v2.article.writer.v4.repair import run_targeted_repairs

        self.assertNotIn("recover_copyright", inspect.getsource(run_targeted_repairs))

    def test_event030_copyright_recovery_not_in_compile(self) -> None:
        import inspect

        from newsagent_v2.article.writer.v4 import compile as compile_mod

        self.assertNotIn("recover_copyright", inspect.getsource(compile_mod.compile_v4_article))

    def test_factbank_and_capacity_regressions(self) -> None:
        props = [
            FactProposition(
                proposition_id=f"P{i}",
                text=f"Proposition number {i} about Deutsche Bank custody plan detail.",
                primary_source_support=True,
                entities=("Deutsche Bank",),
            )
            for i in range(1, 12)
        ]
        bank = FactBank(event_id="e", propositions=props, quotes=[], conflicts=[])
        from newsagent_v2.article.writer.v4.event_research import EventResearchResult

        research = EventResearchResult(
            pack={"event_id": "e", "evidence": []},
            story={"event_id": "e"},
            sources_retrieved=2,
            independent_sources=2,
            primary_sources=1,
            raw_research_words=500,
        )
        depth = assess_evidence_capacity(bank, research=research)
        self.assertEqual(depth.evidence_capacity, "RICH")
        self.assertEqual(depth.article_type, "FULL_ARTICLE")

    def test_unsupported_security_quote_unchanged(self) -> None:
        article = {
            "headline": "Bank waits on crypto custody approval",
            "dek": "Regulators have not cleared the desk.",
            "article_body": (
                "Deutsche Bank is awaiting regulatory approval for crypto custody services "
                "that would initially support Bitcoin and Ether as listed assets."
            ),
            "seo_title": "Bank waits on crypto custody",
            "meta_description": "Deutsche Bank awaits regulatory approval for crypto custody.",
            "slug": "bank-waits-crypto-custody",
            "category": "markets",
            "claims": [],
        }
        article_input = {
            "event_id": "e",
            "evidence": [
                {
                    "extracted_text": (
                        "Deutsche Bank is awaiting regulatory approval for crypto custody services "
                        "that would initially support Bitcoin and Ether as listed assets."
                    )
                }
            ],
        }
        issues, _metrics = check_similarity(article, article_input)
        critical = [i for i in issues if i.get("severity") == SEVERITY_CRITICAL]
        self.assertTrue(critical)

    def test_minor_mechanics_and_seo_are_warnings(self) -> None:
        article = {
            "headline": "DB",
            "dek": "x",
            "article_body": "One complete sentence about markets today.",
            "seo_title": "",
            "meta_description": "short",
            "slug": "BAD_SLUG",
            "category": "not-a-category",
            "keywords": [],
        }
        seo, _ = check_seo(article)
        for issue in seo:
            self.assertEqual(issue["severity"], SEVERITY_WARNING)
        empty_body = {
            "headline": "Valid Headline Here",
            "dek": "x",
            "article_body": "",
            "seo_title": "Valid SEO Title",
            "meta_description": "A meta description that is long enough to satisfy the checker rules.",
            "slug": "valid-slug",
            "category": "markets",
            "keywords": [],
        }
        mech, _ = check_mechanics(empty_body)
        empty_crit = [i for i in mech if i["code"] == "empty_body"]
        self.assertTrue(empty_crit)
        self.assertTrue(all(i["severity"] == SEVERITY_CRITICAL for i in empty_crit))

    def test_qa_result_exposes_counts(self) -> None:
        result = build_qa_result(
            event_id="e",
            issues=[
                {"code": "a", "severity": SEVERITY_WARNING, "message": "w", "module": "seo"},
                {"code": "b", "severity": SEVERITY_CRITICAL, "message": "c", "module": "grounding"},
            ],
            metrics={},
        )
        self.assertEqual(result["critical_count"], 1)
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["warning_codes"], ["a"])
        self.assertFalse(result["qa_publishable"])
        result2 = build_qa_result(
            event_id="e",
            issues=[{"code": "a", "severity": SEVERITY_WARNING, "message": "w", "module": "seo"}],
            metrics={},
        )
        self.assertTrue(result2["qa_publishable"])
        self.assertEqual(result2["critical_count"], 0)

    def test_groq_and_paid_qwen_calls_zero_on_kimi_path(self) -> None:
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
        self.assertFalse(hasattr(writer, "fallback"))


if __name__ == "__main__":
    unittest.main()


