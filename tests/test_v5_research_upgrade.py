"""Mocked tests for NewsAgent V5 research upgrade (stages 1-3)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from newsagent_v2.discovery.provenance import (
    is_primary_provenance,
    normalize_source_role,
    normalize_source_type,
    stable_source_id,
)
from newsagent_v2.discovery.raw_news_item import RawNewsItem
from newsagent_v2.discovery.source_registry import SourceRegistry, default_sources_config_path
from newsagent_v2.article.writer.v4.event_research import research_event, _merge_search_hits
from newsagent_v2.v5_generation.source_expansion_adapter import (
    build_search_fn_for_event,
    default_search_fn_for_story,
    expand_sources_for_event,
)
from newsagent_v2.v5_generation.evidence_dimensions import (
    evaluate_evidence_dimensions,
    dimension_targeted_queries,
    MAX_EXPANSION_ROUNDS,
    MAX_TOTAL_EVIDENCE_ROWS,
)
from newsagent_v2.v5_generation.depth_cost_helpers import (
    DEPTH_FALLBACK_PASS,
    DEPTH_NORMAL_PASS,
    FALLBACK_FLOOR,
    NORMAL_PASS_MIN,
    STANDARD_FALLBACK_MIN,
    apply_depth_fallback,
    evidence_supports_publishable_depth,
)
from newsagent_v2.article_readiness import INTENTIONAL_ARTICLE_WORDS, MIN_EVIDENCE_WORDS
from newsagent_v2.article.qa.policy import PRODUCTION_HARD_MINIMUM_WORDS


def _qa(codes, event_id="evt-1"):
    return {
        "event_id": event_id,
        "critical_failures": [
            {"code": c, "message": c, "severity": "critical", "module": "depth"} for c in codes
        ],
        "warnings": [],
        "metrics": {},
        "qa_passed": not codes,
        "publishable": not codes,
        "qa_publishable": not codes,
        "critical_count": len(codes),
    }


def _item(source_id, source, title, url, *, primary=False, summary=""):
    return RawNewsItem(
        source=source,
        source_id=source_id,
        source_type="official_regulator" if primary else "crypto_publication",
        source_role="primary_evidence" if primary else "discovery",
        source_authority=1.0 if primary else 0.7,
        headline=title,
        description=summary or title,
        canonical_url=url,
        published_at="2026-09-22T00:00:00+00:00",
        entities=["Bitcoin", "SEC"],
        topics=["regulatory"],
        keywords=["bitcoin", "sec", "etf"],
    )


class Stage1RegistryConfigTests(unittest.TestCase):
    def test_default_config_path_loads_sources_with_source_id(self):
        path = default_sources_config_path()
        self.assertTrue(path.is_file(), path)
        registry = SourceRegistry()  # must load config, not only tiny defaults
        enabled = registry.get_enabled()
        self.assertGreaterEqual(len(enabled), 20)
        for src in enabled:
            self.assertTrue(src.source_id)
            self.assertTrue(src.url.startswith("http"))
            self.assertEqual(src.source_type, normalize_source_type(src.source_type))

    def test_provenance_hierarchy_normalization(self):
        self.assertEqual(normalize_source_type("newsroom"), "crypto_publication")
        self.assertEqual(normalize_source_type("regulator"), "official_regulator")
        self.assertEqual(normalize_source_role("primary", source_type="exchange"), "primary_evidence")
        self.assertTrue(is_primary_provenance("official_regulator", "discovery"))
        self.assertTrue(is_primary_provenance("crypto_company", "primary_evidence"))
        self.assertEqual(stable_source_id("CoinDesk", "https://www.coindesk.com/rss"), "coindesk_com")
        self.assertEqual(stable_source_id("CoinDesk", "https://www.coindesk.com/rss", "coindesk"), "coindesk")

    def test_url_domain_dedupe_in_registry_load(self):
        registry = SourceRegistry()
        urls = [s.url.rstrip("/").lower() for s in registry.get_all()]
        self.assertEqual(len(urls), len(set(urls)))


class Stage2SearchWiringTests(unittest.TestCase):
    def test_search_fn_called_in_production_research_path(self):
        calls = {"n": 0}

        def fake_search(queries, working):
            calls["n"] += 1
            return [
                {
                    "source": "SEC",
                    "source_id": "sec_press",
                    "source_type": "official_regulator",
                    "source_role": "primary_evidence",
                    "url": "https://www.sec.gov/news/press-release/demo",
                    "title": "SEC statement on Bitcoin ETF",
                    "summary": "Official confirmation with figures and next steps.",
                    "provenance": "registry_expansion",
                }
            ]

        story = {
            "event_id": "evt-search",
            "representative_title": "Bitcoin ETF Approval",
            "topic": "regulatory",
            "entities": ["Bitcoin", "SEC"],
            "article_input": {
                "event_id": "evt-search",
                "representative_title": "Bitcoin ETF Approval",
                "evidence": [
                    {
                        "source": "CoinDesk",
                        "source_id": "coindesk",
                        "url": "https://www.coindesk.com/article/etf",
                        "title": "Bitcoin ETF Approval",
                        "summary": "Markets react to the approval.",
                        "source_type": "crypto_publication",
                        "source_role": "discovery",
                    }
                ],
            },
        }
        with patch("newsagent_v2.article.writer.v4.event_research.enrich_story", side_effect=lambda s, **k: s):
            result = research_event(story, search_fn=fake_search, max_sources=4)
        self.assertEqual(calls["n"], 1)
        urls = [r.get("url") for r in result.pack.get("evidence") or []]
        self.assertIn("https://www.sec.gov/news/press-release/demo", urls)
        merged = [r for r in result.pack["evidence"] if "sec.gov" in str(r.get("url"))][0]
        self.assertEqual(merged.get("provenance"), "registry_expansion")
        self.assertEqual(merged.get("source_id"), "sec_press")

    def test_default_search_fn_uses_expansion_not_paid_serp(self):
        story = {
            "event_id": "evt-2",
            "representative_title": "Kraken lists token",
            "topic": "exchange",
            "entities": ["Kraken"],
            "article_input": {"evidence": []},
        }
        mock_result_rows = [
            {
                "source": "Kraken Blog",
                "source_id": "kraken_blog",
                "url": "https://blog.kraken.com/post/1",
                "title": "Kraken lists token",
                "summary": "Official listing note",
                "source_type": "exchange",
                "source_role": "primary_evidence",
                "provenance": "registry_expansion",
            }
        ]
        with patch(
            "newsagent_v2.v5_generation.source_expansion_adapter.expand_sources_for_event"
        ) as exp:
            from newsagent_v2.v5_generation.source_expansion_adapter import ExpansionResult

            exp.return_value = ExpansionResult(
                candidates_considered=5,
                sources_matched=1,
                sources_added=mock_result_rows,
                primary_sources_retained=1,
                independent_domains=["blog.kraken.com"],
                failed_sources=[],
                diagnostics={"llm_calls": 0},
            )
            fn = default_search_fn_for_story(story)
            hits = fn(["Kraken lists token"], story)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["provenance"], "registry_expansion")
        exp.assert_called()
        # Ensure no discovery LLM
        self.assertEqual(exp.return_value.diagnostics.get("llm_calls", 0), 0)

    def test_merge_preserves_url_source_provenance(self):
        base = [{"url": "https://a.example/1", "source": "A", "provenance": "seed"}]
        hits = [
            {
                "url": "https://b.example/2",
                "source": "B",
                "source_id": "b",
                "source_type": "official_regulator",
                "source_role": "primary_evidence",
                "provenance": "registry_expansion",
            }
        ]
        merged = _merge_search_hits(base, hits)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1]["provenance"], "registry_expansion")
        self.assertEqual(merged[1]["source_id"], "b")


class Stage3DimensionAndDepthTests(unittest.TestCase):
    def test_dimension_gap_targeted_expansion_and_stop_when_sufficient(self):
        thin = [
            {
                "title": "Bitcoin ETF news",
                "summary": "Markets moved.",
                "url": "https://news.example/1",
                "source": "Wire",
            }
        ]
        before = evaluate_evidence_dimensions(thin, event_title="Bitcoin ETF news")
        self.assertFalse(before["sufficient"])
        self.assertTrue(before["missing"])
        queries = dimension_targeted_queries("Bitcoin ETF news", ["Bitcoin", "SEC"], before["missing"])
        self.assertTrue(queries)
        # queries should not be identical headline-only repeats
        self.assertTrue(any("statement" in q.lower() or "impact" in q.lower() or "timeline" in q.lower() or "next" in q.lower() or "figures" in q.lower() or "background" in q.lower() for q in queries))

        rich = [
            {
                "title": "Bitcoin ETF Approval",
                "summary": (
                    "In 2026 the SEC said it approved the product after 12,400 comments. "
                    "Previously the agency delayed the decision. The move could impact markets. "
                    "Next steps include listing and surveillance sharing."
                ),
                "url": "https://sec.gov/1",
                "source": "SEC",
            },
            {
                "title": "ETF go-live timeline",
                "summary": "Exchange confirmed listing date and percent fee schedule.",
                "url": "https://exchange.example/1",
                "source": "Exchange",
            },
            {
                "title": "Market reaction",
                "summary": "Analysts said implications include liquidity shifts; follow-up reporting expected.",
                "url": "https://wire.example/1",
                "source": "Wire",
            },
            {
                "title": "Background context",
                "summary": "Earlier filings set the background for today's announcement.",
                "url": "https://pub.example/1",
                "source": "Pub",
            },
        ]
        after = evaluate_evidence_dimensions(rich, event_title="Bitcoin ETF Approval")
        self.assertTrue(after["sufficient"], after)

    def test_expand_until_ready_skips_thin_and_backfills(self):
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        from newsagent_v2.discovery.event_clusterer import EventReport, NewsEvent
        from newsagent_v2.v5_generation.source_expansion_adapter import ExpansionResult

        event = NewsEvent(
            event_id="evt-thin",
            canonical_title="Thin story",
            topic="test",
            reports=[
                EventReport(
                    report_id="r1",
                    source="S1",
                    source_id="s1",
                    source_authority=0.5,
                    headline="Thin",
                    url="https://s1.example/thin",
                    published_at="2026-09-22T00:00:00+00:00",
                    retrieved_at="2026-09-22T00:00:00+00:00",
                    description="Short.",
                    entities=[],
                    raw_item_id="r1",
                )
            ],
        )
        pipeline = V5DiscoveryPipeline.__new__(V5DiscoveryPipeline)
        pipeline.source_registry = MagicMock()
        with patch(
            "newsagent_v2.control.make_v5_bridge.expand_sources_for_event",
            return_value=ExpansionResult(
                candidates_considered=3,
                sources_matched=0,
                sources_added=[],
                primary_sources_retained=0,
                independent_domains=[],
                failed_sources=[],
                diagnostics={},
            ),
        ):
            ready, diag = pipeline._expand_until_ready(event)
        self.assertFalse(ready)
        self.assertEqual(diag["final_status"], "FAIL")

    def test_top5_capacity_target_600(self):
        self.assertEqual(INTENTIONAL_ARTICLE_WORDS, 600)
        self.assertGreaterEqual(MIN_EVIDENCE_WORDS, 100)
        self.assertEqual(NORMAL_PASS_MIN, 600)
        self.assertEqual(PRODUCTION_HARD_MINIMUM_WORDS, 600)
        self.assertEqual(STANDARD_FALLBACK_MIN, 500)
        self.assertEqual(FALLBACK_FLOOR, 400)
        self.assertGreaterEqual(MAX_EXPANSION_ROUNDS, 3)
        self.assertGreaterEqual(MAX_TOTAL_EVIDENCE_ROWS, 20)

    def test_600_plus_normal_pass(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 620},
            qa=_qa([]),
            word_count=620,
            recovery_history=[],
            recovery_succeeded=True,
        )
        self.assertEqual(out["depth_status"], DEPTH_NORMAL_PASS)
        self.assertTrue(out["ok"])

    def test_500_599_fallback_after_recovery(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 520},
            qa=_qa(["below_article_minimum_length"]),
            word_count=520,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertEqual(out["depth_status"], DEPTH_FALLBACK_PASS)
        self.assertTrue(out["ok"])
        self.assertFalse(out.get("exceptional_fallback"))

    def test_400_499_exceptional_fallback(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 420},
            qa=_qa(["below_article_minimum_length"]),
            word_count=420,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertEqual(out["depth_status"], DEPTH_FALLBACK_PASS)
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("exceptional_fallback"))

    def test_under_400_hard_block(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 399},
            qa=_qa(["below_article_minimum_length"]),
            word_count=399,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertNotEqual(out["depth_status"], DEPTH_FALLBACK_PASS)

    def test_zero_discovery_llm_in_expansion_path(self):
        registry = MagicMock()
        registry.get_enabled.return_value = []
        collector = MagicMock()
        collector.collect.return_value = (
            [],
            MagicMock(
                sources_attempted=0,
                sources_succeeded=0,
                sources_failed=0,
                raw_items_collected=0,
                errors=[],
            ),
        )
        result = expand_sources_for_event(
            event={"event_id": "e", "representative_title": "T", "canonical_title": "T"},
            event_entities=["Bitcoin"],
            event_topic="market",
            event_reports=[],
            source_registry=registry,
            collector=collector,
        )
        self.assertEqual(result.diagnostics.get("llm_calls", 0), 0)
        self.assertNotIn("kimi", str(result.diagnostics).lower())

    def test_evidence_capacity_helper_uses_600_intent(self):
        ok, reasons = evidence_supports_publishable_depth(
            {
                "evidence_word_count": 200,
                "distinct_source_count": 2,
                "evidence_item_count": 2,
                "intended_article_words": 600,
            }
        )
        self.assertTrue(ok, reasons)
        ok2, reasons2 = evidence_supports_publishable_depth(
            {
                "evidence_word_count": 40,
                "distinct_source_count": 2,
                "evidence_item_count": 2,
                "intended_article_words": 600,
            }
        )
        self.assertFalse(ok2)
        self.assertIn("insufficient_depth_capacity_for_publication", reasons2)


if __name__ == "__main__":
    unittest.main()
