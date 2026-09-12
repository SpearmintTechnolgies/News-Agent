#!/usr/bin/env python3
"""Focused tests for build_research_json fact provenance + asset detection."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_research_json as brj  # noqa: E402


class DetectAssetTests(unittest.TestCase):
    def test_job148_headline_xrp_beats_body_btc(self):
        headline = "Robinhood's UK launch broadens XRP access to retail"
        aggregate = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, giving "
            "British investors zero-fee access to over 50 digital assets including "
            "$XRP, $BTC, $ETH, and more, all within a single investing app."
        )
        asset, coin = brj.detect_asset(headline, aggregate)
        self.assertEqual(asset, "XRP")
        # Existing ASSET_PATTERNS coin id for XRP is CoinGecko "ripple".
        self.assertEqual(coin, "ripple")

    def test_headline_btc_beats_aggregate_xrp(self):
        headline = "BTC ETF inflows hit a weekly record"
        aggregate = "Analysts also mentioned $XRP and Solana in passing."
        asset, coin = brj.detect_asset(headline, aggregate)
        self.assertEqual(asset, "Bitcoin")
        self.assertEqual(coin, "bitcoin")

    def test_no_headline_asset_uses_first_aggregate_occurrence(self):
        headline = "Broker expands digital asset access in Europe"
        # XRP appears before BTC in the body — textual order, not ASSET_PATTERNS order.
        aggregate = (
            "The product gives access to $XRP first, then later mentions $BTC and ETH."
        )
        asset, coin = brj.detect_asset(headline, aggregate)
        self.assertEqual(asset, "XRP")
        self.assertEqual(coin, "ripple")

    def test_multiple_assets_in_headline_textual_order(self):
        headline = "XRP leads while Bitcoin lags in weekly flows"
        aggregate = "Ignore body $BTC noise."
        asset, coin = brj.detect_asset(headline, aggregate)
        self.assertEqual(asset, "XRP")
        self.assertEqual(coin, "ripple")

    def test_no_supported_asset_returns_unknown(self):
        headline = "Exchange expands retail brokerage tools"
        aggregate = "No tickers here, only traditional finance commentary."
        asset, coin = brj.detect_asset(headline, aggregate)
        self.assertEqual(asset, "Unknown")
        self.assertEqual(coin, "")

    def test_zcash_headline(self):
        asset, coin = brj.detect_asset("Zcash mining fleet launches", "")
        self.assertEqual(asset, "Zcash")
        self.assertEqual(coin, "zcash")

    def test_maya_protocol_unknown(self):
        asset, coin = brj.detect_asset("Maya Protocol exploit", "Hack drained liquidity pools.")
        self.assertEqual(asset, "Unknown")
        self.assertEqual(coin, "")

    def test_headline_empty_body_btc_still_detects_btc(self):
        # Supported detection rules still apply — no unconditional Bitcoin fallback,
        # but an explicit body BTC match remains valid.
        asset, coin = brj.detect_asset(
            "Exchange expands retail brokerage tools",
            "Traders piled into BTC after the announcement.",
        )
        self.assertEqual(asset, "Bitcoin")
        self.assertEqual(coin, "bitcoin")

    def test_ethereum_solana_still_work(self):
        self.assertEqual(brj.detect_asset("Ethereum upgrade lands", "")[0], "Ethereum")
        self.assertEqual(brj.detect_asset("Solana throughput record", "")[1], "solana")


class SecondarySourceGateTests(unittest.TestCase):
    def test_climate_junk_rejected(self):
        headline = (
            "Winklevoss-Backed Cypherpunk Launches World's Largest Zcash Mining Fleet"
        )
        source = {
            "url": (
                "https://climat.meteo.gc.ca/climate_normals/station_select_f.html"
                "?txtStationName=qsstcirsversion"
            ),
            "title": "Climate normals station select",
            "snippet": "Government of Canada climate normals",
            "content": "Passer au contenu principal Selection de la langue English",
        }
        self.assertFalse(brj.secondary_source_relevant(headline, source))

    def test_gizmodo_maya_homonym_rejected(self):
        headline = "Maya Protocol Becomes the 16th Crypto Hack Logged in August Alone"
        source = {
            "url": "https://gizmodo.com/download/maya",
            "title": "Download Maya",
            "snippet": "Autodesk Maya 3D software download",
            "content": (
                "As a 3D software solution, Maya serves professionals worldwide to create "
                "animated projects and perform modeling and simulation tasks."
            ),
        }
        self.assertFalse(brj.secondary_source_relevant(headline, source))

    def test_cypherpunk_secondary_passes(self):
        headline = (
            "Winklevoss-Backed Cypherpunk Launches World's Largest Zcash Mining Fleet"
        )
        source = {
            "url": "https://theblock.co/post/cypherpunk-zcash-mining",
            "title": "Cypherpunk Zcash mining fleet",
            "snippet": "Winklevoss-backed firm launches mining",
            "content": "",
        }
        self.assertTrue(brj.secondary_source_relevant(headline, source))

    def test_filter_keeps_primary_drops_junk(self):
        headline = "Maya Protocol Becomes the 16th Crypto Hack Logged in August Alone"
        pick = "https://beincrypto.com/maya-protocol-exploit-halts-btc-swaps/"
        primary = {
            "url": pick,
            "content": "Maya Protocol halted after an exploit drained $1.7 million CACAO.",
        }
        junk = {
            "url": "https://gizmodo.com/download/maya",
            "title": "Download Maya",
            "content": "Autodesk Maya software for animation studios worldwide.",
        }
        kept, rejected = brj.filter_sources_for_facts([primary, junk], headline, pick)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["url"], pick)
        self.assertEqual(len(rejected), 1)
        self.assertIn("gizmodo.com", rejected[0]["url"])

    def test_filter_noop_without_primary_in_pack(self):
        # Unit fixtures often use a pick URL outside the source set — do not gate.
        sources = [
            {"url": "https://alpha.example/a", "content": "volume rose 40%"},
            {"url": "https://beta.example/b", "content": "fees dropped to 0%"},
        ]
        kept, rejected = brj.filter_sources_for_facts(
            sources, "Exchange expands access", "https://news.example/story"
        )
        self.assertEqual(len(kept), 2)
        self.assertEqual(rejected, [])


class SourcedFactsTests(unittest.TestCase):
    def _write_source(self, out_dir: str, name: str, url: str, content: str, ok: bool = True) -> None:
        path = os.path.join(out_dir, name)
        rec = {
            "url": url,
            "source": brj.domain_of(url),
            "ok": ok,
            "words": len(content.split()) if ok else 0,
            "content": content if ok else "",
            "tier": "test",
            "image_url": "",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def _build_with_sources(self, sources: list[tuple[str, str, str, bool]]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "sources")
            os.makedirs(out_dir)
            picks = {
                "picks": [
                    {
                        "pick_index": 1,
                        "headline": "Exchange expands digital asset access for retail",
                        "url": "https://news.example/story",
                        "category": "altcoins",
                        "wp_category_slugs": ["altcoins"],
                        "wp_category_ids": [8],
                    }
                ]
            }
            picks_path = os.path.join(tmp, "picks.json")
            with open(picks_path, "w", encoding="utf-8") as f:
                json.dump(picks, f)
            for name, url, content, ok in sources:
                self._write_source(out_dir, name, url, content, ok=ok)
            out_path = os.path.join(tmp, "raw.json")
            code, doc = brj.build(picks_path, 1, out_dir, out_path)
            # Partial is fine for provenance/asset unit tests (word floor is orthogonal).
            self.assertIn(doc.get("status"), ("ok", "partial"), msg=doc)
            self.assertIn(code, (0, 1), msg=doc)
            self.assertTrue(os.path.isfile(out_path))
            return doc

    def test_sourced_facts_multi_source_fields(self):
        c1 = (
            "Alpha Desk reports that trading volume rose 40% in August after the launch. "
            "Officials said more than 50 assets are now available on the platform."
        )
        c2 = (
            "Beta Wire confirms custody fees dropped to 0% for retail clients this quarter. "
            "Analysts note the firm processed $100 million in crypto transactions recently."
        )
        doc = self._build_with_sources(
            [
                ("aaa.json", "https://alpha.example/a", c1, True),
                ("bbb.json", "https://beta.example/b", c2, True),
            ]
        )
        self.assertIsInstance(doc["combined_key_facts"], list)
        self.assertTrue(all(isinstance(x, str) for x in doc["combined_key_facts"]))
        self.assertGreaterEqual(len(doc["combined_key_facts"]), 2)
        sf = doc.get("sourced_facts") or []
        self.assertGreaterEqual(len(sf), 1)
        first = sf[0]
        self.assertEqual(first["id"], "fact_001")
        self.assertIn("text", first)
        self.assertTrue(first["source_url"].startswith("https://"))
        self.assertTrue(first["source_domain"])
        self.assertTrue(first["source_record"].startswith("research/sources/"))
        self.assertTrue(first["source_record"].endswith(".json"))

    def test_duplicate_fact_keeps_first_source(self):
        shared = (
            "Shared Desk reports trading volume rose 40% in August after the UK launch event. "
            "More than 50 digital assets are listed for zero-fee access today."
        )
        doc = self._build_with_sources(
            [
                ("aaa.json", "https://first.example/a", shared, True),
                ("bbb.json", "https://second.example/b", shared, True),
            ]
        )
        facts = doc["combined_key_facts"]
        self.assertEqual(len([f for f in facts if f[:80] == shared[:80] or f == shared]), 1)
        # Only one provenance entry for the duplicated sentence (first domain wins after load_sources
        # domain dedupe OR seen_facts). Domains differ so both load; seen_facts keeps first.
        texts = [s["text"] for s in doc["sourced_facts"]]
        self.assertEqual(len(texts), len(set(t[:80] for t in texts)))
        match = [s for s in doc["sourced_facts"] if "40%" in s["text"]]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["source_domain"], "first.example")

    def test_failed_source_creates_no_sourced_facts(self):
        good = (
            "Gamma Press reports that trading volume rose 40% after the product launch today. "
            "The desk said $100 million changed hands across more than 50 listed assets."
        )
        doc = self._build_with_sources(
            [
                ("bad.json", "https://dead.example/x", "", False),
                ("good.json", "https://good.example/g", good, True),
                (
                    "good2.json",
                    "https://other.example/o",
                    "Other Desk said custody fees fell to 0% this quarter for retail clients "
                    "while prediction markets revenue rose to $156 million overall.",
                    True,
                ),
            ]
        )
        domains = {s["source_domain"] for s in doc["sourced_facts"]}
        self.assertNotIn("dead.example", domains)

    def test_padding_does_not_fabricate_sourced_url(self):
        # Force material selection to return [] so build() pads combined_key_facts
        # without inventing sourced_facts provenance URLs.
        filler = ("word " * 650).strip() + "."
        original = brj.select_material_facts
        try:
            brj.select_material_facts = lambda *a, **k: []  # type: ignore[assignment]
            doc = self._build_with_sources(
                [
                    ("a.json", "https://a.example/a", filler, True),
                    ("b.json", "https://b.example/b", filler + " extra.", True),
                ]
            )
        finally:
            brj.select_material_facts = original
        self.assertIsInstance(doc["combined_key_facts"], list)
        self.assertTrue(all(isinstance(x, str) for x in doc["combined_key_facts"]))
        self.assertGreaterEqual(len(doc["combined_key_facts"]), 2)
        # Pad/orphan facts must not appear in sourced_facts with fabricated URLs.
        self.assertEqual(doc.get("sourced_facts"), [])
        for item in doc.get("sourced_facts") or []:
            self.assertTrue(item.get("source_url", "").startswith("http"))
            self.assertTrue(item.get("source_domain"))


class ExtractionV16Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.story = {
            "primary_headline": "Robinhood's UK launch broadens XRP access to retail",
            "topic_theme": "Robinhood's UK launch broadens XRP access to retail",
            "primary_keyword": "Robinhood's UK launch",
            "primary_asset": "XRP",
        }

    def test_numeric_news_fact_selectable(self):
        text = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, giving "
            "British investors zero-fee access to over 50 digital assets including $XRP."
        )
        self.assertTrue(brj.is_material_candidate(text, self.story))
        score = brj.materiality_score(
            text, self.story, url="https://bsc.news/news/rh", domain="bsc.news"
        )
        self.assertGreaterEqual(score, 4)

    def test_score0_product_feature_enters_pool(self):
        text = (
            "The launch also introduces Robinhood Cortex Digests for Crypto, a generative "
            "AI-powered feature that analyses breaking news and market data."
        )
        self.assertTrue(brj.is_material_candidate(text, self.story))
        scored = brj.score_candidates(
            text, self.story, url="https://bsc.news/news/rh", domain="bsc.news"
        )
        self.assertTrue(any("Cortex" in c["text"] for c in scored))

    def test_score0_regulatory_enters_pool(self):
        text = (
            "Robinhood UK Ltd secured registration with the FCA as a cryptoasset firm, "
            "confirming it meets the regulator's anti-money laundering standards."
        )
        # May include digits in practice; ensure regulatory path works even without $.
        self.assertTrue(brj.is_material_candidate(text, self.story))
        scored = brj.score_candidates(
            text, self.story, url="https://bsc.news/news/rh", domain="bsc.news"
        )
        self.assertTrue(scored)
        self.assertEqual(scored[0]["category"], "REGULATORY")

    def test_cortex_outranks_homepage_gold_marketing(self):
        cortex = (
            "The launch also introduces Robinhood Cortex Digests for Crypto, a generative "
            "AI-powered feature that analyses breaking news, market data, and technical indicators."
        )
        gold = "Robinhood Gold members get zero management fees on every dollar over $100K."
        sc_cortex = brj.materiality_score(
            cortex, self.story, url="https://bsc.news/news/rh-uk", domain="bsc.news"
        )
        sc_gold = brj.materiality_score(
            gold, self.story, url="https://robinhood.com/", domain="robinhood.com"
        )
        self.assertGreater(sc_cortex, sc_gold)

    def test_dollar_one_cta_demoted(self):
        text = "Get started with as little as $1."
        score = brj.materiality_score(
            text, self.story, url="https://robinhood.com/", domain="robinhood.com"
        )
        self.assertLess(score, 1)
        scored = brj.score_candidates(
            text, self.story, url="https://robinhood.com/", domain="robinhood.com"
        )
        self.assertEqual(scored, [])

    def test_play_store_demoted(self):
        text = "Earn 3.35% APY on uninvested cash (no cap) with 50+ crypto assets available."
        score = brj.materiality_score(
            text,
            self.story,
            url="https://play.google.com/store/apps/details?id=com.robinhood.android",
            domain="play.google.com",
        )
        self.assertLess(score, 1)

    def test_author_bio_demoted(self):
        text = "Soumen has been a crypto researcher since 2020 and holds a master's in Physics."
        score = brj.materiality_score(
            text, self.story, url="https://bsc.news/news/rh", domain="bsc.news"
        )
        self.assertLess(score, 1)

    def test_story_relevance_changes_ranking(self):
        rh = (
            "Robinhood launched crypto trading in the UK via Bitstamp for XRP investors."
        )
        other = (
            "An unrelated mining firm in Asia announced a new warehouse expansion plan today."
        )
        sc_rh = brj.materiality_score(
            rh, self.story, url="https://wire.example/news/a", domain="wire.example"
        )
        sc_other = brj.materiality_score(
            other, self.story, url="https://wire.example/news/b", domain="wire.example"
        )
        self.assertGreater(sc_rh, sc_other)

    def test_soft_diversity_without_forcing_empty(self):
        pool = [
            {"text": "Financial A with $200 million deal closed in 2025 for Robinhood Bitstamp.", "score": 10, "category": "FINANCIAL"},
            {"text": "Financial B with $100 million revenue and 38% decline for Robinhood.", "score": 9, "category": "FINANCIAL"},
            {"text": "Financial C with $50 million fees noted around Robinhood UK launch.", "score": 8, "category": "FINANCIAL"},
            {
                "text": "Robinhood introduces Cortex Digests for Crypto generative AI feature for UK traders.",
                "score": 7,
                "category": "PRODUCT_FEATURE",
            },
            {
                "text": "Robinhood UK Ltd secured FCA registration confirming regulatory compliance standards.",
                "score": 7,
                "category": "REGULATORY",
            },
        ]
        selected = brj.diversify_select(pool, max_total=4)
        cats = {c["category"] for c in selected}
        self.assertIn("PRODUCT_FEATURE", cats)
        self.assertIn("REGULATORY", cats)
        self.assertLessEqual(len(selected), 4)

    def test_final_and_per_source_caps(self):
        self.assertEqual(brj.FINAL_FACT_CAP, 12)
        self.assertEqual(brj.PER_SOURCE_CANDIDATES, 6)
        content = " ".join(
            f"Robinhood launched product feature number {i} with XRP access in the UK market."
            for i in range(20)
        )
        facts = brj.extract_key_facts(
            content,
            max_facts=brj.PER_SOURCE_CANDIDATES,
            story_ctx=self.story,
            url="https://news.example/news/a",
            domain="news.example",
        )
        self.assertLessEqual(len(facts), brj.PER_SOURCE_CANDIDATES)

    def test_job148_style_material_selection_includes_key_topics(self):
        bsc = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, giving British "
            "investors zero-fee access to over 50 digital assets including $XRP, $BTC, $ETH. "
            "The UK offering runs on infrastructure when Robinhood acquired Bitstamp for "
            "$200 million, closing the deal on June 2, 2025. "
            "The launch also introduces Robinhood Cortex Digests for Crypto, a generative "
            "AI-powered feature that analyses breaking news and market data. "
            "Available tokens include $BTC, $ETH, $XRP, Hyperliquid $HYPE, Cardano $ADA, "
            "Chainlink $LINK, and Shiba Inu, among others. "
            "The UK is building out a comprehensive crypto regulatory framework expected to "
            "take effect in 2027, and by securing FCA registration and launching services now, "
            "Robinhood positions itself as an established compliant player. "
            "It is worth noting one key consumer protection caveat: crypto assets held through "
            "Bitstamp UK are not covered by the UK's Financial Services Compensation Scheme."
        )
        home = (
            "Get started with as little as $1. "
            "Robinhood Gold members get zero management fees on every dollar over $100K."
        )
        play = (
            "Earn 3.35% APY on uninvested cash (no cap). "
            "Cryptocurrency services are offered through Robinhood Crypto, LLC (NMLS ID 1702840)."
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "sources")
            os.makedirs(out_dir)
            picks = {
                "picks": [
                    {
                        "pick_index": 1,
                        "headline": "Robinhood's UK launch broadens XRP access to retail",
                        "url": "https://bsc.news/news/robinhood-uk",
                        "category": "altcoins",
                        "wp_category_slugs": ["altcoins"],
                        "wp_category_ids": [8],
                    }
                ]
            }
            picks_path = os.path.join(tmp, "picks.json")
            with open(picks_path, "w", encoding="utf-8") as f:
                json.dump(picks, f)
            for name, url, content in (
                ("bsc.json", "https://bsc.news/news/robinhood-uk-crypto", bsc),
                ("home.json", "https://robinhood.com/", home),
                (
                    "play.json",
                    "https://play.google.com/store/apps/details?id=com.robinhood.android",
                    play,
                ),
            ):
                with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "url": url,
                            "source": brj.domain_of(url),
                            "ok": True,
                            "words": len(content.split()),
                            "content": content,
                            "tier": "t",
                            "image_url": "",
                        },
                        f,
                    )
            _code, doc = brj.build(picks_path, 1, out_dir, os.path.join(tmp, "raw.json"))
            texts = " ".join(doc["combined_key_facts"]).lower()
            self.assertIsInstance(doc["combined_key_facts"], list)
            self.assertTrue(all(isinstance(x, str) for x in doc["combined_key_facts"]))
            self.assertLessEqual(len(doc["sourced_facts"]), brj.FINAL_FACT_CAP)
            self.assertIn("cortex", texts)
            self.assertTrue(
                "fca" in texts or "2027" in texts,
                msg="expected regulatory FCA/2027 context",
            )
            self.assertTrue(
                "cardano" in texts or "chainlink" in texts or "$ada" in texts or "$link" in texts,
                msg="expected asset-availability tickers",
            )
            self.assertNotIn("as little as $1", texts)
            self.assertNotIn("gold members", texts.lower())
            self.assertNotIn("3.35%", texts)
            for sf in doc["sourced_facts"]:
                self.assertTrue(sf["source_url"].startswith("http"))
                self.assertTrue(sf["source_domain"])
                self.assertTrue(sf["source_record"].startswith("research/sources/"))


if __name__ == "__main__":
    unittest.main()
