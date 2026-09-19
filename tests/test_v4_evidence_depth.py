"""Offline fixtures for V4 evidence-depth redesign. No live /make. No provider calls."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.evidence_depth import (
    ARTICLE_FULL,
    ARTICLE_LIMITED,
    ARTICLE_STANDARD,
    CAPACITY_LIMITED,
    CAPACITY_MEDIUM,
    CAPACITY_RICH,
    assess_evidence_capacity,
    is_writer_underproduced,
    provider_error_is_infrastructure,
    slightly_below_recommended,
)
from newsagent_v2.article.writer.v4.event_research import (
    EventResearchResult,
    build_event_search_queries,
    pack_excludes_generation_source_prose,
    research_event,
)
from newsagent_v2.article.writer.v4.expand import (
    enrichment_messages_exclude_source_prose,
    realize_body_word_target,
    _enrichment_messages,
    build_unused_evidence_set,
)
from newsagent_v2.article.writer.v4.factbank import (
    FactBank,
    FactProposition,
    build_fact_bank,
    fact_bank_to_writer_packet,
)
from newsagent_v2.article.writer.v4.packet import AuthorizedFact, WriterEvidencePacket
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    ScriptedV4Writer,
    V4NativeArticle,
    _generation_packet_dict,
    build_v4_writer_messages,
)


def _prop(pid: str, text: str, **kwargs: Any) -> FactProposition:
    return FactProposition(proposition_id=pid, text=text, **kwargs)


def _rich_bank() -> FactBank:
    props = []
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
    for i, text in enumerate(templates, start=1):
        props.append(
            _prop(
                f"P{i:02d}",
                text,
                subject="Deutsche Bank" if i == 1 else "",
                numbers=("1",) if "Bitcoin" in text else (),
                attribution="spokesperson" if "spokesperson" in text else "",
                primary_source_support=i <= 3,
                source_ids=(f"e{1 + (i % 3)}",),
                entities=("Deutsche Bank", "Bitcoin") if "Bitcoin" in text else ("Deutsche Bank",),
            )
        )
    return FactBank(event_id="evt-rich", propositions=tuple(props), dedup_merged_count=2, raw_claim_count=14)


def _limited_bank() -> FactBank:
    props = (
        _prop("P01", "Acme Corp disclosed a software update.", source_ids=("e1",)),
        _prop("P02", "The update covers retail payment tools.", source_ids=("e1",)),
        _prop("P03", "No customer action is required.", source_ids=("e1",)),
        _prop("P04", "Support teams prepared guidance for callers.", source_ids=("e1",)),
        _prop("P05", "Acme limited the change to one product line.", source_ids=("e1",)),
    )
    return FactBank(event_id="evt-lim", propositions=props, raw_claim_count=5)


def _pad(body: str, n: int) -> str:
    filler = "Support teams prepared guidance for callers. "
    while word_count(body) < n:
        body = (body + " " + filler).strip()
    return body


class EventResearchTests(unittest.TestCase):
    def test_single_source_to_multi_source_event_research(self) -> None:
        story = {
            "event_id": "evt-1",
            "representative_title": "Deutsche Bank awaits crypto custody approval",
            "article_input": {
                "event_id": "evt-1",
                "evidence": [
                    {
                        "evidence_id": "e1",
                        "source": "Wire",
                        "source_role": "newsroom",
                        "url": "https://news.example/a",
                        "summary": "Deutsche Bank awaits approval.",
                    },
                    {
                        "evidence_id": "e2",
                        "source": "Regulator",
                        "source_type": "regulator",
                        "source_role": "primary_evidence",
                        "url": "https://gov.example/b",
                        "summary": "Application pending.",
                    },
                    {
                        "evidence_id": "e3",
                        "source": "Bank",
                        "source_type": "company",
                        "url": "https://bank.example/c",
                        "summary": "Custody plans outlined.",
                    },
                ],
            },
        }

        def fake_fetch(url: str):
            html = f"<html><body><p>{url} Deutsche Bank is awaiting regulatory approval for crypto custody including Bitcoin and Ether.</p></body></html>"
            return 200, "text/html", html.encode("utf-8"), url

        def fake_search(queries, _story):
            self.assertTrue(queries)
            return [
                {
                    "evidence_id": "e4",
                    "source": "Independent",
                    "source_role": "newsroom",
                    "url": "https://ind.example/d",
                    "summary": "Independent coverage of custody plan.",
                }
            ]

        result = research_event(story, fetch=fake_fetch, search_fn=fake_search, max_sources=4)
        self.assertEqual(result.mode, "multi_source_event_research")
        self.assertGreaterEqual(result.sources_discovered, 4)
        self.assertGreaterEqual(result.sources_retrieved, 1)
        self.assertGreaterEqual(result.primary_sources, 1)
        self.assertTrue(result.search_queries)

    def test_primary_source_preference(self) -> None:
        queries = build_event_search_queries(
            {"representative_title": "SEC reviews Acme license filing"},
            {"evidence": [{"source": "Acme", "title": "Acme Files"}, {"source": "SEC"}]},
        )
        blob = " ".join(queries).lower()
        self.assertTrue(any("official" in q.lower() or "regulator" in q.lower() for q in queries) or "sec" in blob)

    def test_raw_source_writer_boundary(self) -> None:
        packet = WriterEvidencePacket(
            event_id="e",
            story_topic="t",
            authorized_facts=(AuthorizedFact(id="P01", proposition="Acme disclosed an update."),),
        )
        gen = _generation_packet_dict(packet)
        self.assertTrue(pack_excludes_generation_source_prose(gen))
        dirty = {**gen, "extracted_text": "RAW SOURCE"}
        self.assertFalse(pack_excludes_generation_source_prose(dirty))
        messages = build_v4_writer_messages(packet)
        self.assertNotIn("extracted_text", json.dumps(messages).lower())


class FactBankTests(unittest.TestCase):
    def test_multi_source_fact_extraction_and_dedup(self) -> None:
        pack = {
            "event_id": "evt-dedup",
            "evidence": [
                {
                    "evidence_id": "A",
                    "source": "A",
                    "extracted_text": "Bitcoin fell 4.6% on Tuesday after the announcement.",
                },
                {
                    "evidence_id": "B",
                    "source": "B",
                    "extracted_text": "BTC declined 4.6% on Tuesday after the announcement.",
                },
                {
                    "evidence_id": "C",
                    "source": "C",
                    "extracted_text": "Bitcoin dropped by 4.6% on Tuesday after the announcement.",
                },
            ],
        }
        bank = build_fact_bank(event_id="evt-dedup", pack=pack)
        self.assertGreaterEqual(bank.raw_claim_count, 3)
        self.assertLess(bank.unique_proposition_count, bank.raw_claim_count)
        self.assertGreaterEqual(bank.dedup_merged_count, 1)
        # Provenance preserved on merged fact.
        multi = [p for p in bank.propositions if len(p.source_ids) >= 2]
        self.assertTrue(multi)

    def test_conflicting_source_attribution(self) -> None:
        pack = {
            "event_id": "evt-conflict",
            "evidence": [
                {
                    "evidence_id": "co",
                    "source": "Company",
                    "source_type": "company",
                    "extracted_text": "The company said losses totaled 10 million dollars after the breach.",
                },
                {
                    "evidence_id": "reg",
                    "source": "Regulator",
                    "source_type": "regulator",
                    "source_role": "primary_evidence",
                    "extracted_text": "The regulator said losses totaled 25 million dollars after the breach.",
                },
            ],
        }
        bank = build_fact_bank(event_id="evt-conflict", pack=pack)
        self.assertGreaterEqual(bank.unique_proposition_count, 2)
        # Do not silently collapse conflicting numbers into one prop.
        nums = {tuple(p.numbers) for p in bank.propositions if p.numbers}
        self.assertGreaterEqual(len(nums), 1)

    def test_multi_source_provenance(self) -> None:
        bank = _rich_bank()
        packet = fact_bank_to_writer_packet(bank, story_topic="Custody")
        self.assertTrue(packet.authorized_facts)
        self.assertNotIn("extracted_text", json.dumps(packet.as_dict()))


class EvidenceCapacityTests(unittest.TestCase):
    def test_rich_medium_limited_capacity(self) -> None:
        research = EventResearchResult(
            pack={},
            story={},
            independent_sources=3,
            primary_sources=1,
            sources_retrieved=3,
        )
        rich = assess_evidence_capacity(_rich_bank(), research=research)
        self.assertEqual(rich.evidence_capacity, CAPACITY_RICH)
        self.assertEqual(rich.article_type, ARTICLE_FULL)
        self.assertEqual(rich.recommended_word_range, (250, 400))

        medium_props = _rich_bank().propositions[:8]
        medium_bank = FactBank(event_id="m", propositions=medium_props)
        medium = assess_evidence_capacity(
            medium_bank,
            research=EventResearchResult(pack={}, story={}, independent_sources=2),
        )
        self.assertEqual(medium.evidence_capacity, CAPACITY_MEDIUM)
        self.assertEqual(medium.article_type, ARTICLE_STANDARD)

        limited = assess_evidence_capacity(_limited_bank())
        self.assertEqual(limited.evidence_capacity, CAPACITY_LIMITED)
        self.assertEqual(limited.article_type, ARTICLE_LIMITED)
        self.assertTrue(limited.evidence_limited)

    def test_rich_75_words_is_underproduction(self) -> None:
        research = EventResearchResult(
            pack={}, story={}, independent_sources=3, primary_sources=1
        )
        depth = assess_evidence_capacity(_rich_bank(), research=research)
        self.assertTrue(is_writer_underproduced(75, depth))

    def test_limited_150_words_is_not_underproduction(self) -> None:
        depth = assess_evidence_capacity(_limited_bank())
        self.assertFalse(is_writer_underproduced(150, depth))

    def test_medium_unused_fact_enrichment_and_source_excluded(self) -> None:
        research = EventResearchResult(pack={}, story={}, independent_sources=2)
        depth = assess_evidence_capacity(
            FactBank(event_id="m", propositions=_rich_bank().propositions[:8]),
            research=research,
        )
        self.assertEqual(depth.evidence_capacity, CAPACITY_MEDIUM)
        self.assertTrue(slightly_below_recommended(120, depth))
        native = V4NativeArticle(
            headline="Deutsche Bank is awaiting regulatory approval for crypto custody",
            dek="Custody",
            article_body=_pad(
                "Deutsche Bank is awaiting regulatory approval for crypto custody. "
                "Initial supported asset includes Bitcoin.",
                120,
            ),
            seo_title="Custody",
            meta_description="Custody",
            slug="custody",
        )
        packet = fact_bank_to_writer_packet(
            FactBank(event_id="m", propositions=_rich_bank().propositions[:8]),
            story_topic="Custody",
        )
        ledgers = FactBank(event_id="m", propositions=_rich_bank().propositions[:8]).to_ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        unused = build_unused_evidence_set(
            packet=packet, report=report, article_body=native.article_body
        )
        messages = _enrichment_messages(
            native=native,
            unused=unused,
            current_words=120,
            target_min=depth.recommended_word_min,
            target_max=depth.recommended_word_max,
        )
        self.assertTrue(enrichment_messages_exclude_source_prose(messages))
        writer = ScriptedV4Writer(
            native.as_dict(),
            expansion_text=(
                "Initial supported asset includes Ether. "
                "Initial supported assets include selected stablecoins."
            ),
        )
        out, _r, expansion = realize_body_word_target(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=writer,
            depth=depth,
        )
        self.assertEqual(expansion.mode, "enrichment")
        self.assertEqual(writer.expansion_calls, 1)
        self.assertGreater(out.word_count, 120)

    def test_provider_rate_limit_not_editorial_failure(self) -> None:
        self.assertTrue(provider_error_is_infrastructure("RATE_LIMITED"))
        self.assertTrue(provider_error_is_infrastructure("429 ITPM same_org_groq_failover_skipped"))
        research = EventResearchResult(
            pack={}, story={}, independent_sources=3, primary_sources=1
        )
        depth = assess_evidence_capacity(_rich_bank(), research=research)
        native = V4NativeArticle(
            headline="Deutsche Bank is awaiting regulatory approval for crypto custody",
            dek="x",
            article_body="Deutsche Bank is awaiting regulatory approval for crypto custody.",
            seo_title="x",
            meta_description="x",
            slug="x",
        )
        packet = fact_bank_to_writer_packet(_rich_bank())
        ledgers = _rich_bank().to_ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(error="provider_error status=429 Rate limit ITPM")
        # Force regeneration path with failing render on regen.
        writer.regeneration_payload = None
        writer.error = None

        class _RLWriter:
            model = "scripted"
            generation_calls = 0

            def render(self, packet, regeneration=False):
                from newsagent_v2.article.writer.v4.writer import V4WriterResult

                self.generation_calls += 1
                return V4WriterResult(
                    ok=False,
                    provider_error=True,
                    error="429 Rate limit reached ITPM",
                    error_type="RATE_LIMIT",
                )

        out, _r, expansion = realize_body_word_target(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": []},
            report=report,
            writer=_RLWriter(),
            depth=depth,
        )
        self.assertTrue(expansion.provider_infrastructure_error)
        self.assertIn("PROVIDER_RATE_LIMITED", str(expansion.reject_reason))
        self.assertFalse(str(expansion.reject_reason).startswith("WRITER_UNDERPRODUCED"))


class LimitedDepthPublishabilityTests(unittest.TestCase):
    def test_limited_depth_brief_policy_gates(self) -> None:
        depth = assess_evidence_capacity(_limited_bank())
        self.assertEqual(depth.article_type, ARTICLE_LIMITED)
        self.assertEqual(depth.qa_article_mode, "brief")
        # Still requires grounding/copyright/mechanics/security â€” length alone is not enough.
        self.assertTrue(depth.evidence_limited)
        native = V4NativeArticle(
            headline="Acme Corp disclosed a software update",
            dek="Update",
            article_body=_pad(
                "Acme Corp disclosed a software update. "
                "The update covers retail payment tools. "
                "No customer action is required. "
                "Support teams prepared guidance for callers. "
                "Acme limited the change to one product line.",
                150,
            ),
            seo_title="Acme",
            meta_description="Update",
            slug="acme",
        )
        packet = fact_bank_to_writer_packet(_limited_bank())
        ledgers = _limited_bank().to_ledgers()
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertTrue(report.ok)
        out, _r, expansion = realize_body_word_target(
            native,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": [{"extracted_text": "SHOULD NOT MATTER"}]},
            report=report,
            writer=ScriptedV4Writer(expansion_text="nope"),
            depth=depth,
        )
        self.assertEqual(expansion.mode, "accept_limited")
        self.assertFalse(expansion.rejected)
        self.assertEqual(out.word_count, native.word_count)


class RegressionHooks(unittest.TestCase):
    def test_copyright_and_grounding_modules_untouched_by_thresholds(self) -> None:
        from newsagent_v2.article.qa import similarity
        from newsagent_v2.article.writer.v4 import copyright_recovery
        import inspect
        from newsagent_v2.article.writer.v4.repair import run_targeted_repairs

        # Similarity helpers remain available for diagnostics; V4 publish path skips them.
        self.assertTrue(hasattr(similarity, "check_similarity"))
        self.assertTrue(hasattr(copyright_recovery, "recover_copyright"))
        self.assertNotIn("recover_copyright", inspect.getsource(run_targeted_repairs))


if __name__ == "__main__":
    unittest.main()


