#!/usr/bin/env python3
"""Deterministic tests for Phase 3 cross-claim consistency."""
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

import check_claim_consistency as cc  # noqa: E402


def _ver(*claims: dict) -> dict:
    return {
        "verification_version": 1,
        "status": "ok",
        "mode": "deep_research",
        "summary": {
            "total": len(claims),
            "direct": sum(1 for c in claims if c["status"] == "DIRECT"),
            "corroborated": sum(1 for c in claims if c["status"] == "CORROBORATED"),
            "single_source": sum(1 for c in claims if c["status"] == "SINGLE_SOURCE"),
            "conflicted": 0,
            "unsupported": 0,
        },
        "claims": list(claims),
    }


def _claim(
    cid: str,
    text: str,
    *,
    status: str = "SINGLE_SOURCE",
    domain: str = "news.example",
) -> dict:
    return {
        "claim_id": cid,
        "claim": text,
        "origin_url": f"https://{domain}/{cid}",
        "origin_domain": domain,
        "status": status,
        "supporting_domains": [domain],
        "evidence": [
            {
                "source_url": f"https://{domain}/{cid}",
                "source_domain": domain,
                "support_type": "DIRECT",
            }
        ],
    }


class ConsistencyCoreTests(unittest.TestCase):
    def test_job148_bitstamp_year_mismatch_potential(self):
        a = _claim(
            "fact_002",
            "The UK offering runs on infrastructure when Robinhood acquired Bitstamp "
            "for $200 million, closing the deal on June 2, 2025.",
        )
        b = _claim(
            "fact_006",
            "The rollout is powered by Bitstamp UK Ltd, the firm Robinhood acquired "
            "for $200 million in 2024.",
            domain="wire.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(doc["summary"]["potential_conflict"], 1)
        self.assertEqual(doc["summary"]["likely_conflict"], 0)
        self.assertEqual(len(doc["issues"]), 1)
        issue = doc["issues"][0]
        self.assertEqual(issue["severity"], "POTENTIAL_CONFLICT")
        self.assertEqual(issue["rule"], "ENTITY_MONEY_YEAR_MISMATCH")
        self.assertEqual(issue["claim_ids"], ["fact_002", "fact_006"])
        self.assertIn("2025", issue["conflicting_values"])
        self.assertIn("2024", issue["conflicting_values"])
        anchors = {x.lower() for x in issue["shared_anchors"]}
        self.assertTrue({"robinhood", "bitstamp"} <= anchors)

    def test_different_entities_same_money_no_conflict(self):
        a = _claim(
            "fact_a",
            "Robinhood acquired Bitstamp for $200 million in 2025 after regulatory clearance.",
        )
        b = _claim(
            "fact_b",
            "Coinbase acquired NexusTrust for $200 million in 2024 after a separate review.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(doc["issues"], [])
        self.assertEqual(doc["summary"]["potential_conflict"], 0)

    def test_same_entity_different_money_scale_no_year_rule(self):
        a = _claim(
            "fact_a",
            "Robinhood acquired Bitstamp for $200 million in 2025 according to filings.",
        )
        b = _claim(
            "fact_b",
            "Robinhood acquired Bitstamp for $200 thousand in 2024 according to a blog.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        year_issues = [i for i in doc["issues"] if i["rule"] == "ENTITY_MONEY_YEAR_MISMATCH"]
        self.assertEqual(year_issues, [])

    def test_approved_vs_rejected_likely(self):
        a = _claim(
            "fact_a",
            "The SEC approved the Spot Ethereum ETF application from BlackRock this week.",
        )
        b = _claim(
            "fact_b",
            "The SEC rejected the Spot Ethereum ETF application from BlackRock this week.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(len(doc["issues"]), 1)
        self.assertEqual(doc["issues"][0]["severity"], "LIKELY_CONFLICT")
        self.assertEqual(doc["issues"][0]["rule"], "APPROVED_VS_REJECTED")

    def test_launched_vs_cancelled_likely(self):
        a = _claim(
            "fact_a",
            "Robinhood launched crypto trading for UK retail customers via Bitstamp rails.",
        )
        b = _claim(
            "fact_b",
            "Robinhood cancelled crypto trading for UK retail customers via Bitstamp rails.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(len(doc["issues"]), 1)
        self.assertEqual(doc["issues"][0]["severity"], "LIKELY_CONFLICT")
        self.assertEqual(doc["issues"][0]["rule"], "LAUNCHED_VS_CANCELLED")

    def test_listed_vs_delisted_likely(self):
        a = _claim(
            "fact_a",
            "Binance listed the SOL token for spot trading across major markets today.",
        )
        b = _claim(
            "fact_b",
            "Binance delisted the SOL token for spot trading across major markets today.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(len(doc["issues"]), 1)
        self.assertEqual(doc["issues"][0]["severity"], "LIKELY_CONFLICT")
        self.assertEqual(doc["issues"][0]["rule"], "LISTED_VS_DELISTED")

    def test_generic_uk_trading_no_conflict(self):
        a = _claim("fact_a", "UK trading activity rose after the weekend session closed quietly.")
        b = _claim(
            "fact_b",
            "UK investors waited for guidance after the weekend session closed quietly.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(doc["issues"], [])

    def test_one_shared_weak_anchor_no_conflict(self):
        a = _claim(
            "fact_a",
            "Trading investors watched the market network after fees changed in 2025.",
        )
        b = _claim(
            "fact_b",
            "Trading investors watched the market network after fees changed in 2024.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        self.assertEqual(doc["issues"], [])

    def test_same_entity_money_no_years_no_mismatch(self):
        a = _claim(
            "fact_a",
            "Robinhood acquired Bitstamp for $200 million after regulators cleared the path.",
        )
        b = _claim(
            "fact_b",
            "Robinhood acquired Bitstamp for $200 million following a lengthy review cycle.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        year_issues = [i for i in doc["issues"] if i["rule"] == "ENTITY_MONEY_YEAR_MISMATCH"]
        self.assertEqual(year_issues, [])

    def test_same_year_different_money_no_year_mismatch(self):
        a = _claim(
            "fact_a",
            "Robinhood acquired Bitstamp for $200 million in 2025 after clearance.",
        )
        b = _claim(
            "fact_b",
            "Robinhood acquired Bitstamp for $50 million in 2025 after clearance.",
            domain="other.example",
        )
        doc = cc.check_consistency(_ver(a, b))
        year_issues = [i for i in doc["issues"] if i["rule"] == "ENTITY_MONEY_YEAR_MISMATCH"]
        self.assertEqual(year_issues, [])

    def test_conflicted_and_unsupported_excluded(self):
        a = _claim(
            "fact_002",
            "Robinhood acquired Bitstamp for $200 million in 2025 after the board vote.",
        )
        b = _claim(
            "fact_006",
            "Robinhood acquired Bitstamp for $200 million in 2024 after the board vote.",
            domain="other.example",
        )
        b["status"] = "CONFLICTED"
        c = _claim(
            "fact_x",
            "Robinhood acquired Bitstamp for $200 million in 2023 after the board vote.",
            domain="third.example",
        )
        c["status"] = "UNSUPPORTED"
        doc = cc.check_consistency(_ver(a, b, c))
        self.assertEqual(doc["summary"]["claims_compared"], 1)
        self.assertEqual(doc["issues"], [])

    def test_empty_issues_valid(self):
        a = _claim(
            "fact_a",
            "Volume rose after the product update according to the latest desk note.",
        )
        doc = cc.check_consistency(_ver(a))
        self.assertEqual(doc["issues"], [])
        self.assertTrue(cc.consistency_ok(doc, ["fact_a"]))
        self.assertEqual(doc["summary"]["claims_compared"], 1)
        self.assertEqual(doc["summary"]["pairs_checked"], 0)
        self.assertEqual(doc["summary"]["none"], 0)

    def test_summary_counts_consistent(self):
        a = _claim(
            "fact_002",
            "Robinhood acquired Bitstamp for $200 million in 2025 after clearance.",
        )
        b = _claim(
            "fact_006",
            "Robinhood acquired Bitstamp for $200 million in 2024 after clearance.",
            domain="other.example",
        )
        c = _claim(
            "fact_007",
            "A 0.1% FX fee applies on weekdays according to the brokerage notice.",
            domain="fees.example",
        )
        doc = cc.check_consistency(_ver(a, b, c))
        s = doc["summary"]
        self.assertEqual(s["none"] + s["potential_conflict"] + s["likely_conflict"], s["pairs_checked"])
        self.assertEqual(s["potential_conflict"] + s["likely_conflict"], len(doc["issues"]))
        self.assertTrue(cc.consistency_ok(doc, ["fact_002", "fact_006", "fact_007"]))

    def test_run_consistency_writes_artifact(self):
        a = _claim(
            "fact_002",
            "Robinhood acquired Bitstamp for $200 million in 2025 after clearance.",
        )
        b = _claim(
            "fact_006",
            "Robinhood acquired Bitstamp for $200 million in 2024 after clearance.",
            domain="other.example",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ver_path = os.path.join(tmp, "verification.json")
            out_path = os.path.join(tmp, "claim_consistency.json")
            with open(ver_path, "w", encoding="utf-8") as f:
                json.dump(_ver(a, b), f)
            code, doc = cc.run_consistency(ver_path, out_path)
            self.assertEqual(code, 0)
            self.assertTrue(os.path.isfile(out_path))
            self.assertEqual(doc["issues"][0]["severity"], "POTENTIAL_CONFLICT")
            # resume
            code2, doc2 = cc.run_consistency(ver_path, out_path)
            self.assertEqual(code2, 0)
            self.assertEqual(doc2["issues"][0]["issue_id"], doc["issues"][0]["issue_id"])


if __name__ == "__main__":
    unittest.main()
