#!/usr/bin/env python3
"""Focused deterministic tests for Claim Verification V1."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import verify_claims as vc  # noqa: E402


def _write_source(dir_path: str, name: str, url: str, content: str, ok: bool = True) -> None:
    rec = {
        "url": url,
        "source": vc.domain_of(url),
        "ok": ok,
        "words": len(content.split()) if ok else 0,
        "content": content if ok else "",
        "tier": "test",
    }
    with open(os.path.join(dir_path, name), "w", encoding="utf-8") as f:
        json.dump(rec, f)


class VerifyClaimsCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sources = os.path.join(self.tmp.name, "sources")
        self.verify_cache = os.path.join(self.tmp.name, "verify_sources")
        os.makedirs(self.sources)
        os.makedirs(self.verify_cache)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _validated(self, facts: list[dict], **extra) -> dict:
        urls = list({f["source_url"] for f in facts if f.get("source_url")})
        doc = {
            "status": "ok",
            "primary_headline": extra.get(
                "headline", "Robinhood UK launch broadens XRP access to retail"
            ),
            "topic_theme": extra.get("headline", "Robinhood UK launch broadens XRP access"),
            "primary_keyword": "Robinhood UK launch",
            "primary_asset": extra.get("asset", "XRP"),
            "source_urls": extra.get("source_urls", urls),
            "sourced_facts": facts,
            "combined_key_facts": extra.get(
                "combined_key_facts",
                [f["text"] for f in facts] + (extra.get("pad_facts") or []),
            ),
        }
        return doc

    def test_origin_supports_direct(self):
        claim = (
            "Robinhood has launched crypto trading in the UK via Bitstamp giving "
            "investors zero-fee access to over 50 digital assets."
        )
        _write_source(
            self.sources,
            "a.json",
            "https://alpha.example/story",
            claim + " More context about the brokerage rollout follows here.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://beta.example/other",
            "Unrelated market commentary without the Robinhood UK launch details at all.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://alpha.example/story",
                    "source_domain": "alpha.example",
                    "source_record": "research/sources/a.json",
                }
            ]
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["claims"][0]["status"], "DIRECT")
        self.assertEqual(doc["summary"]["direct"], 1)

    def test_origin_plus_second_domain_corroborated(self):
        claim = (
            "Trading volume rose 40% in August after the UK product launch event."
        )
        body = claim + " Analysts said retail flow improved across more than 50 assets."
        _write_source(self.sources, "a.json", "https://first.example/a", body)
        _write_source(self.sources, "b.json", "https://second.example/b", body)
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://first.example/a",
                    "source_domain": "first.example",
                    "source_record": "research/sources/a.json",
                }
            ]
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["claims"][0]["status"], "CORROBORATED")
        self.assertIn("second.example", doc["claims"][0]["supporting_domains"])

    def test_single_source_after_search_no_help(self):
        claim = (
            "The exchange launched a new XRP listing after regulatory approval in August."
        )
        _write_source(
            self.sources,
            "a.json",
            "https://only.example/a",
            claim + " Additional desk notes follow without other outlets.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://noise.example/b",
            "Weather delayed shipping logistics across Europe this week.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://only.example/a",
                    "source_domain": "only.example",
                    "source_record": "research/sources/a.json",
                }
            ],
            source_urls=["https://only.example/a", "https://noise.example/b"],
        )

        def fake_search(query, max_results=6, **kwargs):
            return [
                {
                    "title": "Unrelated bond yields",
                    "url": "https://other.example/bonds",
                    "snippet": "Treasury yields moved higher on inflation data.",
                    "domain": "other.example",
                }
            ]

        def fake_read(url, out_dir, source=None):
            return {
                "url": url,
                "ok": True,
                "content": "Bond markets rallied as traders priced rate cuts.",
                "words": 10,
                "tier": "test",
                "source": source or "other",
            }

        doc = vc.verify_claims(
            validated,
            self.sources,
            verify_cache_dir=self.verify_cache,
            search_fn=fake_search,
            read_fn=fake_read,
            enable_search=True,
        )
        self.assertEqual(doc["claims"][0]["status"], "SINGLE_SOURCE")

    def test_unsupported(self):
        claim = "Revenue increased 40% after the product launch in August."
        _write_source(
            self.sources,
            "a.json",
            "https://a.example/a",
            "Completely unrelated prose about weather and shipping delays only.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://b.example/b",
            "More unrelated commentary without any revenue figures at all here.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://a.example/a",
                    "source_domain": "a.example",
                    "source_record": "research/sources/a.json",
                }
            ]
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["claims"][0]["status"], "UNSUPPORTED")

    def test_conflicted_numeric(self):
        claim = "Company revenue increased 40% in the latest quarter."
        origin = (
            "Company revenue increased 40% in the latest quarter according to filings."
        )
        other = (
            "Filings show company revenue increased 12% in the latest quarter overall."
        )
        _write_source(self.sources, "a.json", "https://a.example/a", origin)
        _write_source(self.sources, "b.json", "https://b.example/b", other)
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://a.example/a",
                    "source_domain": "a.example",
                    "source_record": "research/sources/a.json",
                }
            ]
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["claims"][0]["status"], "CONFLICTED")

    def test_origin_domain_not_corroboration(self):
        claim = "Trading volume rose 40% after the launch event."
        body = claim + " Desk notes confirm the same volume figure for August."
        _write_source(self.sources, "a1.json", "https://same.example/one", body)
        _write_source(self.sources, "a2.json", "https://same.example/two", body)
        # load_source_corpus keeps both files but domain lists — corroboration skips same domain
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://same.example/one",
                    "source_domain": "same.example",
                    "source_record": "research/sources/a1.json",
                }
            ],
            source_urls=["https://same.example/one", "https://same.example/two"],
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["claims"][0]["status"], "DIRECT")
        self.assertEqual(doc["claims"][0]["supporting_domains"], ["same.example"])

    def test_same_domain_not_corroboration_alias(self):
        # Explicit: second URL same domain must not yield CORROBORATED
        self.test_origin_domain_not_corroboration()

    def test_cached_source_prevents_second_read(self):
        claim = (
            "The exchange launched XRP trading after regulatory approval this month."
        )
        _write_source(
            self.sources,
            "a.json",
            "https://only.example/a",
            claim + " Local color about the desk follows without partners.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://noise.example/n",
            "Sports scores and weather updates for the weekend period.",
        )
        # Pre-seed verify cache for candidate URL
        cand_url = "https://fresh.example/story"
        _write_source(
            self.verify_cache,
            vc.sha1_url(cand_url) + ".json",
            cand_url,
            claim + " Independent outlet confirms the exchange launched XRP trading.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://only.example/a",
                    "source_domain": "only.example",
                    "source_record": "research/sources/a.json",
                }
            ],
            source_urls=["https://only.example/a", "https://noise.example/n"],
        )
        reads: list[str] = []

        def fake_search(query, max_results=6, **kwargs):
            return [
                {
                    "title": claim,
                    "url": cand_url,
                    "snippet": claim,
                    "domain": "fresh.example",
                }
            ]

        def fake_read(url, out_dir, source=None):
            reads.append(url)
            return {"url": url, "ok": True, "content": "x", "words": 1, "tier": "t"}

        doc = vc.verify_claims(
            validated,
            self.sources,
            verify_cache_dir=self.verify_cache,
            search_fn=fake_search,
            read_fn=fake_read,
            enable_search=True,
        )
        self.assertEqual(reads, [])
        self.assertEqual(doc["claims"][0]["status"], "CORROBORATED")

    def test_search_ranking_uses_overlap_threshold(self):
        claim = "Robinhood launched UK crypto trading with zero-fee XRP access."
        _write_source(
            self.sources,
            "a.json",
            "https://only.example/a",
            claim + " Extra sentence for support of the originating article body.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://noise.example/n",
            "Unrelated shipping and logistics news without crypto brokerage details.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://only.example/a",
                    "source_domain": "only.example",
                    "source_record": "research/sources/a.json",
                }
            ],
            source_urls=["https://only.example/a", "https://noise.example/n"],
        )
        read_urls: list[str] = []

        def fake_search(query, max_results=6, **kwargs):
            return [
                {
                    "title": "Potato harvest rises",
                    "url": "https://food.example/potatoes",
                    "snippet": "Farmers report a strong potato harvest this autumn.",
                    "domain": "food.example",
                },
                {
                    "title": claim,
                    "url": "https://good.example/rh",
                    "snippet": claim,
                    "domain": "good.example",
                },
            ]

        def fake_read(url, out_dir, source=None):
            read_urls.append(url)
            return {
                "url": url,
                "ok": True,
                "content": claim + " Confirmed by a second desk.",
                "words": 20,
                "tier": "t",
                "source": source or "",
            }

        doc = vc.verify_claims(
            validated,
            self.sources,
            verify_cache_dir=self.verify_cache,
            search_fn=fake_search,
            read_fn=fake_read,
            enable_search=True,
        )
        self.assertNotIn("https://food.example/potatoes", read_urls)
        self.assertTrue(any("good.example" in u for u in read_urls) or doc["claims"][0]["status"] == "CORROBORATED")

    def test_known_source_url_skipped_from_search(self):
        claim = "The firm launched a new listing after approval from regulators."
        _write_source(
            self.sources,
            "a.json",
            "https://only.example/a",
            claim + " Originating coverage continues with market reaction notes.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://known.example/k",
            "Unrelated known source filler without the launch claim text present.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://only.example/a",
                    "source_domain": "only.example",
                    "source_record": "research/sources/a.json",
                }
            ],
            source_urls=["https://only.example/a", "https://known.example/k"],
        )
        read_urls: list[str] = []

        def fake_search(query, max_results=6, **kwargs):
            return [
                {
                    "title": claim,
                    "url": "https://known.example/k",
                    "snippet": claim,
                    "domain": "known.example",
                }
            ]

        def fake_read(url, out_dir, source=None):
            read_urls.append(url)
            return {"url": url, "ok": True, "content": claim, "words": 10, "tier": "t"}

        doc = vc.verify_claims(
            validated,
            self.sources,
            verify_cache_dir=self.verify_cache,
            search_fn=fake_search,
            read_fn=fake_read,
            enable_search=True,
        )
        self.assertEqual(read_urls, [])
        self.assertEqual(doc["claims"][0]["status"], "SINGLE_SOURCE")

    def test_extra_fetch_limit_respected(self):
        claims = []
        for i in range(5):
            text = (
                f"Exchange {i} launched a new token listing after regulatory approval "
                f"with $100 million volume."
            )
            claims.append(
                {
                    "id": f"fact_{i+1:03d}",
                    "text": text,
                    "source_url": f"https://o{i}.example/a",
                    "source_domain": f"o{i}.example",
                    "source_record": f"research/sources/o{i}.json",
                }
            )
            _write_source(
                self.sources,
                f"o{i}.json",
                f"https://o{i}.example/a",
                text + " Origin body continues with more launch detail.",
            )
            _write_source(
                self.sources,
                f"n{i}.json",
                f"https://n{i}.example/n",
                "Unrelated noise article without launch figures for this desk.",
            )

        validated = self._validated(
            claims,
            source_urls=[c["source_url"] for c in claims]
            + [f"https://n{i}.example/n" for i in range(5)],
        )
        reads: list[str] = []

        def fake_search(query, max_results=6, **kwargs):
            # unique domain per query hash
            tag = abs(hash(query)) % 100000
            return [
                {
                    "title": query,
                    "url": f"https://ext{tag}.example/x",
                    "snippet": query,
                    "domain": f"ext{tag}.example",
                }
            ]

        def fake_read(url, out_dir, source=None):
            reads.append(url)
            return {
                "url": url,
                "ok": True,
                "content": "No helpful overlap content about potatoes only.",
                "words": 8,
                "tier": "t",
            }

        vc.verify_claims(
            validated,
            self.sources,
            verify_cache_dir=self.verify_cache,
            search_fn=fake_search,
            read_fn=fake_read,
            enable_search=True,
        )
        self.assertLessEqual(len(reads), vc.MAX_FETCHES_PER_JOB)

    def test_padded_combined_key_facts_ignored(self):
        claim = "Volume rose 40% after launch according to the desk note today."
        _write_source(
            self.sources,
            "a.json",
            "https://a.example/a",
            claim + " More originating support text for the volume figure.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://b.example/b",
            claim + " Independent confirmation of the same volume rise figure.",
        )
        pad = "word " * 40
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://a.example/a",
                    "source_domain": "a.example",
                    "source_record": "research/sources/a.json",
                }
            ],
            pad_facts=[pad.strip()],
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(len(doc["claims"]), 1)
        self.assertEqual(doc["claims"][0]["claim_id"], "fact_001")

    def test_summary_counts_match(self):
        claim = "Volume rose 40% after the launch according to filings."
        _write_source(self.sources, "a.json", "https://a.example/a", claim + " Origin.")
        _write_source(self.sources, "b.json", "https://b.example/b", claim + " Other.")
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://a.example/a",
                    "source_domain": "a.example",
                    "source_record": "research/sources/a.json",
                }
            ]
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["summary"], vc.summarize(doc["claims"]))
        self.assertTrue(vc.validation_ok(doc, ["fact_001"]))

    def test_job148_style_fact(self):
        claim = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, giving "
            "British investors zero-fee access to over 50 digital assets including XRP."
        )
        _write_source(
            self.sources,
            "a.json",
            "https://news.example/robinhood-uk",
            claim + " The brokerage framed the rollout as a major European expansion.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://wire.example/robinhood",
            claim + " Rival desks also noted the UK launch and XRP access.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://news.example/robinhood-uk",
                    "source_domain": "news.example",
                    "source_record": "research/sources/a.json",
                }
            ],
            headline="Robinhood UK launch broadens XRP access to retail",
            asset="XRP",
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        self.assertEqual(doc["claims"][0]["status"], "CORROBORATED")
        self.assertEqual(doc["claims"][0]["origin_domain"], "news.example")


class FilterHelpersTests(unittest.TestCase):
    def test_filter_drops_known_and_low_overlap(self):
        results = [
            {
                "title": "Potato news",
                "url": "https://food.example/p",
                "snippet": "Harvest",
                "domain": "food.example",
            },
            {
                "title": "Robinhood UK launch XRP",
                "url": "https://known.example/k",
                "snippet": "Robinhood UK launch XRP access",
                "domain": "known.example",
            },
            {
                "title": "Robinhood UK launch broadens XRP access",
                "url": "https://fresh.example/f",
                "snippet": "Robinhood UK launch broadens XRP access to retail traders",
                "domain": "fresh.example",
            },
        ]
        claim = "Robinhood UK launch broadens XRP access to retail"
        out = vc.filter_and_rank_candidates(
            results,
            claim=claim,
            origin_url="https://origin.example/o",
            known_urls={"https://known.example/k"},
            origin_domain="origin.example",
        )
        urls = [r["url"] for r in out]
        self.assertNotIn("https://known.example/k", urls)
        self.assertIn("https://fresh.example/f", urls)


class Phase2ParaphraseTests(unittest.TestCase):
    """Paraphrase-aware corroboration for LONG claims only; threshold stays 0.8."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sources = os.path.join(self.tmp.name, "sources")
        self.verify_cache = os.path.join(self.tmp.name, "verify_sources")
        os.makedirs(self.sources)
        os.makedirs(self.verify_cache)
        self.story_ctx = {
            "primary_headline": "Robinhood UK launch broadens XRP access to retail",
            "topic_theme": "Robinhood UK launch broadens XRP access",
            "primary_keyword": "Robinhood UK launch",
            "primary_asset": "XRP",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _validated(self, facts: list[dict], **extra) -> dict:
        urls = list({f["source_url"] for f in facts if f.get("source_url")})
        return {
            "status": "ok",
            "primary_headline": extra.get(
                "headline", "Robinhood UK launch broadens XRP access to retail"
            ),
            "topic_theme": extra.get("headline", "Robinhood UK launch broadens XRP access"),
            "primary_keyword": "Robinhood UK launch",
            "primary_asset": extra.get("asset", "XRP"),
            "source_urls": extra.get("source_urls", urls),
            "sourced_facts": facts,
            "combined_key_facts": [f["text"] for f in facts],
        }

    def test_token_support_threshold_unchanged(self):
        self.assertEqual(vc.TOKEN_SUPPORT_THRESHOLD, 0.8)
        self.assertEqual(vc.SOFT_PARAPHRASE_OVERLAP, 0.45)
        self.assertEqual(vc.LONG_CLAIM_MIN_TOKENS, 8)

    def test_job148_cryptopotato_paraphrase_corroborated(self):
        claim = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, "
            "giving investors access to over 50 digital assets."
        )
        paraphrase = (
            "Robinhood launched cryptocurrency trading for UK investors "
            "through its Bitstamp-powered service, with more than 50 assets."
        )
        self.assertTrue(vc.is_long_claim(claim))
        self.assertFalse(vc.claim_directly_supported(claim, paraphrase))
        ok, meta = vc.support_with_meta(claim, paraphrase, story_ctx=self.story_ctx)
        self.assertTrue(ok)
        self.assertEqual(meta["match_rule"], "ANCHOR_PARAPHRASE")
        self.assertEqual(meta["support_type"], "CORROBORATING")
        anchors_l = {a.lower() for a in meta["matched_anchors"]}
        self.assertTrue({"robinhood", "bitstamp"} <= anchors_l)

        _write_source(
            self.sources,
            "a.json",
            "https://cryptopotato.example/rh",
            claim + " Extra originating coverage of the UK brokerage rollout.",
        )
        _write_source(
            self.sources,
            "b.json",
            "https://wire.example/rh-uk",
            paraphrase + " Analysts framed the move as a European expansion.",
        )
        validated = self._validated(
            [
                {
                    "id": "fact_001",
                    "text": claim,
                    "source_url": "https://cryptopotato.example/rh",
                    "source_domain": "cryptopotato.example",
                    "source_record": "research/sources/a.json",
                }
            ]
        )
        doc = vc.verify_claims(
            validated, self.sources, verify_cache_dir=self.verify_cache, enable_search=False
        )
        row = doc["claims"][0]
        self.assertEqual(row["status"], "CORROBORATED")
        para_ev = [
            e
            for e in row["evidence"]
            if e.get("match_rule") == "ANCHOR_PARAPHRASE"
        ]
        self.assertEqual(len(para_ev), 1)
        self.assertEqual(para_ev[0]["support_type"], "CORROBORATING")
        self.assertIn("matched_anchors", para_ev[0])

    def test_short_claim_dollar_one_not_corroborated(self):
        claim = "Get started with as little as $1."
        evidence = "I started a $1 million business."
        self.assertFalse(vc.is_long_claim(claim))
        ok, meta = vc.support_with_meta(claim, evidence, story_ctx=self.story_ctx)
        self.assertFalse(ok)
        self.assertIsNone(meta)
        self.assertFalse(vc.claim_directly_supported(claim, evidence))

    def test_long_claim_entity_event_strong_number_corroborated(self):
        claim = (
            "Coinbase acquired the custody firm NexusTrust in 2025 after "
            "raising more than $200 million for the deal."
        )
        evidence = (
            "Coinbase completed its acquisition of NexusTrust during 2025, "
            "backed by over $200 million in financing for the transaction."
        )
        self.assertTrue(vc.is_long_claim(claim))
        self.assertFalse(vc.claim_directly_supported(claim, evidence))
        ok, meta = vc.support_with_meta(claim, evidence, story_ctx={})
        self.assertTrue(ok)
        self.assertEqual(meta["match_rule"], "ANCHOR_PARAPHRASE")

    def test_long_claim_only_one_shared_entity_not_corroborated(self):
        claim = (
            "Robinhood expanded European retail access after regulators cleared "
            "the brokerage for a wider crypto product suite."
        )
        evidence = (
            "Bitstamp quietly upgraded settlement rails while a different firm "
            "pushed unrelated custody tooling across several markets."
        )
        ok, meta = vc.support_with_meta(claim, evidence, story_ctx=self.story_ctx)
        self.assertFalse(ok)
        self.assertIsNone(meta)

    def test_long_claim_generics_plus_dollar_one_not_corroborated(self):
        claim = (
            "Retail crypto market investors and network users can launch trading "
            "access with as little as $1 on the service app."
        )
        evidence = (
            "A founder later started a $1 million business after leaving the "
            "brokerage desk last spring in another country."
        )
        self.assertTrue(vc.is_long_claim(claim))
        self.assertFalse(vc.claim_directly_supported(claim, evidence))
        ok, meta = vc.support_with_meta(claim, evidence, story_ctx={})
        self.assertFalse(ok)
        self.assertIsNone(meta)

    def test_money_same_scale_compatible(self):
        a = vc.extract_typed_numbers("$200 million")
        b = vc.extract_typed_numbers("about $200 million raised")
        self.assertTrue(vc.numbers_compatible(a, b))

    def test_money_different_scale_incompatible(self):
        a = vc.extract_typed_numbers("$200 million")
        b = vc.extract_typed_numbers("$200 thousand")
        self.assertFalse(vc.numbers_compatible(a, b))

    def test_percent_same_compatible(self):
        a = vc.extract_typed_numbers("volume rose 38%")
        b = vc.extract_typed_numbers("a 38% increase")
        self.assertTrue(vc.numbers_compatible(a, b))

    def test_percent_different_incompatible(self):
        a = vc.extract_typed_numbers("volume rose 38%")
        b = vc.extract_typed_numbers("only 12% growth")
        self.assertFalse(vc.numbers_compatible(a, b))

    def test_count_plus_compatible_with_plain(self):
        a = vc.extract_typed_numbers("over 50+ digital assets listed")
        b = vc.extract_typed_numbers("more than 50 assets available")
        self.assertTrue(vc.numbers_compatible(a, b))

    def test_multiple_entities_unrelated_event_not_corroborated(self):
        claim = (
            "Robinhood partnered with Bitstamp to launch crypto trading for "
            "UK investors with over 50 assets."
        )
        evidence = (
            "Robinhood sued Bitstamp in a custody dispute involving more than "
            "50 unrelated wallet accounts across several jurisdictions."
        )
        # Shared entities exist; no shared event stem and soft overlap stays below 0.45.
        ok, meta = vc.support_with_meta(claim, evidence, story_ctx=self.story_ctx)
        self.assertFalse(ok)
        self.assertIsNone(meta)

    def test_existing_strict_substring_still_works(self):
        claim = (
            "Robinhood has launched crypto trading in the UK via Bitstamp giving "
            "investors zero-fee access to over 50 digital assets."
        )
        evidence = "Preface. " + claim + " Closing remarks from the desk."
        self.assertTrue(vc.claim_directly_supported(claim, evidence))
        ok, meta = vc.support_with_meta(claim, evidence, story_ctx=self.story_ctx)
        self.assertTrue(ok)
        self.assertEqual(meta["match_rule"], "STRICT")
        self.assertEqual(meta["support_type"], "DIRECT")

    def test_short_claim_matching_remains_strict(self):
        claim = "Volume rose 40%."
        paraphrase = "Trading activity climbed by forty percent after the listing."
        self.assertFalse(vc.is_long_claim(claim))
        self.assertFalse(vc.claim_directly_supported(claim, paraphrase))
        ok, _ = vc.support_with_meta(claim, paraphrase, story_ctx={})
        self.assertFalse(ok)
        exact = "Desk note: Volume rose 40%. End."
        self.assertTrue(vc.claim_directly_supported(claim, exact))


if __name__ == "__main__":
    unittest.main()
