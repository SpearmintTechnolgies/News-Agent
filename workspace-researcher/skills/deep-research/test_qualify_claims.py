#!/usr/bin/env python3
"""Deterministic tests for Phase 1.5 claim qualification."""
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

import qualify_claims as qc  # noqa: E402
import verify_claims as vc  # noqa: E402


def _validated(facts: list[dict], **extra) -> dict:
    return {
        "status": "ok",
        "primary_headline": extra.get(
            "headline",
            "Robinhood's UK launch broadens XRP access to retail",
        ),
        "topic_theme": extra.get(
            "theme", "Robinhood UK launch broadens XRP access"
        ),
        "primary_keyword": extra.get("keyword", "Robinhood UK launch"),
        "primary_asset": extra.get("asset", "XRP"),
        "source_urls": extra.get("source_urls", [f["source_url"] for f in facts]),
        "sourced_facts": facts,
        "combined_key_facts": [f["text"] for f in facts],
    }


def _fact(fid: str, text: str, url: str, domain: str) -> dict:
    return {
        "id": fid,
        "text": text,
        "source_url": url,
        "source_domain": domain,
        "source_record": f"research/sources/{fid}.json",
    }


class QualifyClaimsTests(unittest.TestCase):
    def test_get_started_dollar_one_skip_marketing(self):
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_004",
                        "Get started with as little as $1.",
                        "https://robinhood.com/",
                        "robinhood.com",
                    )
                ]
            )
        )
        c = doc["claims"][0]
        self.assertEqual(c["decision"], "SKIP")
        self.assertEqual(c["reason_code"], "MARKETING_CTA")

    def test_play_store_skip(self):
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_009",
                        "Robinhood has launched crypto trading in the UK via Bitstamp.",
                        "https://play.google.com/store/apps/details?id=com.robinhood.android",
                        "play.google.com",
                    )
                ]
            )
        )
        self.assertEqual(doc["claims"][0]["decision"], "SKIP")
        self.assertEqual(doc["claims"][0]["reason_code"], "APP_STORE_CONTENT")

    def test_homepage_generic_promo_skip(self):
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_010",
                        "Robinhood Gold members get zero management fees on every dollar over $100K.",
                        "https://robinhood.com/",
                        "robinhood.com",
                    )
                ]
            )
        )
        c = doc["claims"][0]
        self.assertEqual(c["decision"], "SKIP")
        self.assertIn(c["reason_code"], ("HOMEPAGE_BOILERPLATE", "MARKETING_CTA", "NON_MATERIAL"))

    def test_short_legitimate_news_verify(self):
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_011",
                        "SEC approved the ETF.",
                        "https://coindesk.com/markets/sec-etf",
                        "coindesk.com",
                    )
                ],
                headline="SEC approved the spot ETF filing",
                theme="SEC ETF approval",
                keyword="SEC ETF",
                asset="Bitcoin",
            )
        )
        self.assertEqual(doc["claims"][0]["decision"], "VERIFY")
        self.assertEqual(doc["claims"][0]["reason_code"], "MATERIAL_CLAIM")

    def test_concrete_fee_relevant_verify(self):
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_007",
                        "However, a 0.1% foreign exchange (FX) fee will be applied during weekdays, rising to 0.3% on weekends.",
                        "https://coinalertnews.com/news/2026/08/10/robinhood-crypto-trading-uk-launch",
                        "coinalertnews.com",
                    )
                ]
            )
        )
        self.assertEqual(doc["claims"][0]["decision"], "VERIFY")
        self.assertEqual(doc["claims"][0]["reason_code"], "MATERIAL_CLAIM")

    def test_long_factual_launch_verify(self):
        text = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, giving "
            "British investors zero-fee access to over 50 digital assets including $XRP."
        )
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_001",
                        text,
                        "https://bsc.news/news/robinhood-uk-crypto-bitstamp-xrp-shib",
                        "bsc.news",
                    )
                ]
            )
        )
        self.assertEqual(doc["claims"][0]["decision"], "VERIFY")

    def test_generic_company_promo_skip(self):
        doc = qc.qualify_claims(
            _validated(
                [
                    _fact(
                        "fact_012",
                        "Download the app and join now to unlock your money's potential today.",
                        "https://examplebroker.com/",
                        "examplebroker.com",
                    )
                ]
            )
        )
        self.assertEqual(doc["claims"][0]["decision"], "SKIP")

    def test_sourced_facts_unchanged(self):
        facts = [
            _fact(
                "fact_004",
                "Get started with as little as $1.",
                "https://robinhood.com/",
                "robinhood.com",
            ),
            _fact(
                "fact_001",
                "Robinhood has launched crypto trading in the UK via Bitstamp for XRP access.",
                "https://bsc.news/news/x",
                "bsc.news",
            ),
        ]
        validated = _validated(facts)
        before = json.dumps(validated["sourced_facts"])
        qc.qualify_claims(validated)
        after = json.dumps(validated["sourced_facts"])
        self.assertEqual(before, after)
        self.assertEqual(len(validated["sourced_facts"]), 2)

    def test_summary_counts(self):
        facts = [
            _fact(
                "fact_004",
                "Get started with as little as $1.",
                "https://robinhood.com/",
                "robinhood.com",
            ),
            _fact(
                "fact_001",
                "Robinhood has launched crypto trading in the UK via Bitstamp for XRP access.",
                "https://bsc.news/news/x",
                "bsc.news",
            ),
            _fact(
                "fact_009",
                "Users love the charts in the Robinhood mobile experience.",
                "https://play.google.com/store/apps/details?id=com.robinhood.android",
                "play.google.com",
            ),
        ]
        doc = qc.qualify_claims(_validated(facts))
        self.assertEqual(doc["summary"], qc.summarize(doc["claims"]))
        self.assertEqual(doc["summary"]["total"], 3)
        self.assertEqual(
            doc["summary"]["verify"] + doc["summary"]["skip"],
            doc["summary"]["total"],
        )
        self.assertGreaterEqual(doc["summary"]["skip"], 2)
        self.assertGreaterEqual(doc["summary"]["verify"], 1)

    def test_verify_receives_only_verify_claims(self):
        facts = [
            _fact(
                "fact_004",
                "Get started with as little as $1.",
                "https://robinhood.com/",
                "robinhood.com",
            ),
            _fact(
                "fact_001",
                "Robinhood has launched crypto trading in the UK via Bitstamp for XRP access.",
                "https://bsc.news/news/x",
                "bsc.news",
            ),
        ]
        validated = _validated(facts)
        qual = qc.qualify_claims(validated)
        filtered, ids = vc._filter_validated_to_verify(validated, qual)
        filtered_ids = {f["id"] for f in filtered["sourced_facts"]}
        self.assertNotIn("fact_004", filtered_ids)
        self.assertIn("fact_001", filtered_ids)
        self.assertEqual(set(ids), filtered_ids)

        with tempfile.TemporaryDirectory() as tmp:
            sources = os.path.join(tmp, "sources")
            os.makedirs(sources)
            body = facts[1]["text"] + " Extra context for origin support."
            with open(os.path.join(sources, "a.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "url": facts[1]["source_url"],
                        "ok": True,
                        "content": body,
                        "words": 20,
                        "source": "bsc",
                        "tier": "t",
                    },
                    f,
                )
            out = vc.verify_claims(
                filtered,
                sources,
                enable_search=False,
                qualification_source="research/qualified_claims.json",
            )
            claim_ids = [c["claim_id"] for c in out["claims"]]
            self.assertEqual(claim_ids, ["fact_001"])
            self.assertEqual(out.get("qualification_source"), "research/qualified_claims.json")

    def test_all_skip_succeeds(self):
        facts = [
            _fact(
                "fact_004",
                "Get started with as little as $1.",
                "https://robinhood.com/",
                "robinhood.com",
            ),
            _fact(
                "fact_009",
                "Download now to trade smarter every day.",
                "https://play.google.com/store/apps/details?id=x",
                "play.google.com",
            ),
        ]
        validated = _validated(facts)
        qual = qc.qualify_claims(validated)
        self.assertEqual(qual["summary"]["verify"], 0)
        self.assertEqual(qual["status"], "ok")
        filtered, ids = vc._filter_validated_to_verify(validated, qual)
        self.assertEqual(ids, [])
        with tempfile.TemporaryDirectory() as tmp:
            sources = os.path.join(tmp, "sources")
            os.makedirs(sources)
            out = vc.verify_claims(filtered, sources, enable_search=False)
            self.assertEqual(out["status"], "ok")
            self.assertEqual(out["summary"]["total"], 0)
            self.assertEqual(out["claims"], [])

    def test_qualification_failure_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "missing.json")
            out = os.path.join(tmp, "qualified_claims.json")
            code, doc = qc.run_qualify(bad, out)
            self.assertEqual(code, 1)
            self.assertEqual(doc.get("status"), "error")


class DrainHelperLogicTests(unittest.TestCase):
    """Mirror continue_feed_drain resume expectations without importing the drain module."""

    def test_resume_requires_qualification_for_verify_ids(self):
        validated = _validated(
            [
                _fact(
                    "fact_004",
                    "Get started with as little as $1.",
                    "https://robinhood.com/",
                    "robinhood.com",
                ),
                _fact(
                    "fact_001",
                    "Robinhood has launched crypto trading in the UK via Bitstamp for XRP access.",
                    "https://bsc.news/news/x",
                    "bsc.news",
                ),
            ]
        )
        qual = qc.qualify_claims(validated)
        self.assertTrue(qc.qualification_ok(qual, ["fact_004", "fact_001"]))
        filtered, verify_ids = vc._filter_validated_to_verify(validated, qual)
        ver = {
            "verification_version": 1,
            "status": "ok",
            "mode": "offline_first",
            "qualification_source": "research/qualified_claims.json",
            "claims": [
                {
                    "claim_id": "fact_001",
                    "claim": facts_text if (facts_text := filtered["sourced_facts"][0]["text"]) else "",
                    "origin_url": "https://bsc.news/news/x",
                    "origin_domain": "bsc.news",
                    "status": "DIRECT",
                    "supporting_domains": ["bsc.news"],
                    "evidence": [],
                }
            ],
            "summary": {
                "total": 1,
                "direct": 1,
                "corroborated": 0,
                "single_source": 0,
                "conflicted": 0,
                "unsupported": 0,
            },
        }
        self.assertTrue(vc.validation_ok(ver, verify_ids))
        # Old behavior (all sourced ids) must NOT match when marketing was skipped
        self.assertFalse(vc.validation_ok(ver, ["fact_004", "fact_001"]))


if __name__ == "__main__":
    unittest.main()
