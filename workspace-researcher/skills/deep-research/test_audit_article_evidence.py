#!/usr/bin/env python3
"""Deterministic tests for Phase 4 article evidence audit."""
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

import audit_article_evidence as aae  # noqa: E402


def _ver(*claims: dict) -> dict:
    return {
        "verification_version": 1,
        "status": "ok",
        "mode": "deep_research",
        "summary": {"total": len(claims)},
        "claims": list(claims),
    }


def _rc(
    cid: str,
    text: str,
    *,
    status: str = "CORROBORATED",
) -> dict:
    return {
        "claim_id": cid,
        "claim": text,
        "origin_url": f"https://news.example/{cid}",
        "origin_domain": "news.example",
        "status": status,
        "supporting_domains": ["news.example"],
        "evidence": [],
    }


def _article_shell(body: str) -> str:
    return (
        "META\n"
        "- SEO Title: Example Title With 50 Assets\n"
        "- Primary Keyword: Example Title\n"
        "---\n\n"
        "# Example Title With 50 Assets\n\n"
        f"{body}\n\n"
        "## Conclusion\n\n"
        "Closing sentence without inventing new figures.\n\n"
        "## FAQs\n\n"
        "**1. Invented FAQ?**\n\n"
        "FAQ says Robinhood acquired Bitstamp for $999 billion in 1999.\n\n"
        "**Sources:**\n"
        "- https://news.example/a\n\n"
        "[Word Count: 1100]\n"
    )


class ArticleAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validated = {
            "primary_headline": "Robinhood UK launch Bitstamp XRP",
            "topic_theme": "Robinhood UK launch",
            "primary_keyword": "Robinhood UK launch",
            "primary_asset": "XRP",
        }
        self.research_exact = (
            "Robinhood has launched crypto trading in the UK via Bitstamp, "
            "giving investors access to over 50 digital assets including XRP."
        )

    def test_exact_verified_claim_supported(self):
        md = _article_shell(
            "Robinhood has launched crypto trading in the UK via Bitstamp, "
            "giving investors access to over 50 digital assets including XRP."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        self.assertGreaterEqual(doc["summary"]["supported"], 1)
        statuses = {c["audit_status"] for c in doc["claims"]}
        self.assertIn("SUPPORTED", statuses)
        hit = next(c for c in doc["claims"] if c["matched_claim_id"] == "fact_001")
        self.assertEqual(hit["audit_status"], "SUPPORTED")
        self.assertEqual(hit["match_rule"], "STRICT")

    def test_paraphrase_long_claim_supported(self):
        md = _article_shell(
            "Robinhood launched cryptocurrency trading for UK investors through its "
            "Bitstamp-powered service, with more than 50 assets available on the app."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        matched = [c for c in doc["claims"] if c.get("matched_claim_id") == "fact_001"]
        self.assertTrue(matched)
        self.assertIn(matched[0]["audit_status"], ("SUPPORTED", "SINGLE_SOURCE_REF"))

    def test_new_factual_claim_unmapped(self):
        md = _article_shell(
            "NexusTrust secretly acquired MoonPay for $3 billion in 2021 after a private auction."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        invent = [
            c
            for c in doc["claims"]
            if "moonpay" in c["article_claim"].lower() or "nexustrust" in c["article_claim"].lower()
        ]
        self.assertTrue(invent)
        self.assertTrue(all(c["audit_status"] == "UNMAPPED" for c in invent))

    def test_number_mismatch(self):
        research = (
            "Robinhood acquired Bitstamp for $100 million in 2025 after regulatory clearance."
        )
        md = _article_shell(
            "Robinhood acquired Bitstamp for $150 million in 2025 after regulatory clearance."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_002", research)),
            validated=self.validated,
        )
        mm = [c for c in doc["claims"] if c["audit_status"] == "NUMBER_MISMATCH"]
        self.assertTrue(mm)
        self.assertEqual(mm[0]["matched_claim_id"], "fact_002")

    def test_year_mismatch(self):
        research = (
            "Robinhood acquired Bitstamp for $200 million in 2025 after regulatory clearance."
        )
        md = _article_shell(
            "Robinhood acquired Bitstamp for $200 million in 2024 after regulatory clearance."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_002", research)),
            validated=self.validated,
        )
        ym = [c for c in doc["claims"] if c["audit_status"] == "YEAR_MISMATCH"]
        self.assertTrue(ym)
        self.assertEqual(ym[0]["matched_claim_id"], "fact_002")

    def test_single_source_ref(self):
        md = _article_shell(self.research_exact)
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact, status="SINGLE_SOURCE")),
            validated=self.validated,
        )
        hit = next(c for c in doc["claims"] if c["matched_claim_id"] == "fact_001")
        self.assertEqual(hit["audit_status"], "SINGLE_SOURCE_REF")

    def test_conflict_ref(self):
        research = (
            "Robinhood acquired Bitstamp for $200 million, closing the deal on June 2, 2025."
        )
        md = _article_shell(
            "Robinhood acquired Bitstamp for $200 million, closing the deal on June 2, 2025."
        )
        consistency = {
            "consistency_version": 1,
            "status": "ok",
            "summary": {
                "claims_compared": 2,
                "pairs_checked": 1,
                "none": 0,
                "potential_conflict": 1,
                "likely_conflict": 0,
            },
            "issues": [
                {
                    "issue_id": "consistency_001",
                    "severity": "POTENTIAL_CONFLICT",
                    "rule": "ENTITY_MONEY_YEAR_MISMATCH",
                    "claim_ids": ["fact_002", "fact_006"],
                }
            ],
        }
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_002", research, status="SINGLE_SOURCE")),
            validated=self.validated,
            consistency=consistency,
        )
        hit = next(c for c in doc["claims"] if c["matched_claim_id"] == "fact_002")
        self.assertEqual(hit["audit_status"], "CONFLICT_REF")
        self.assertTrue(hit["consistency_refs"])

    def test_headings_meta_sources_wordcount_excluded(self):
        md = (
            "META\n"
            "- SEO Title: Fake $900 billion acquisition in 1990\n"
            "---\n\n"
            "# Fake $900 billion acquisition in 1990\n\n"
            "## Fake $900 billion acquisition in 1990\n\n"
            "Robinhood has launched crypto trading in the UK via Bitstamp, "
            "giving investors access to over 50 digital assets including XRP.\n\n"
            "## Conclusion\n\n"
            "The launch expands access for investors.\n\n"
            "## FAQs\n\n"
            "**1. Q?**\n\n"
            "Fake FAQ with $900 billion in 1990.\n\n"
            "**Sources:**\n"
            "- Fake $900 billion in 1990\n\n"
            "[Word Count: 1100]\n"
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        joined = " ".join(c["article_claim"] for c in doc["claims"]).lower()
        self.assertNotIn("900 billion", joined)
        self.assertNotIn("1990", joined)

    def test_faq_does_not_affect_main_count(self):
        md = _article_shell(
            "Robinhood has launched crypto trading in the UK via Bitstamp, "
            "giving investors access to over 50 digital assets including XRP."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        # FAQ invents $999 billion / 1999 — must not appear
        for c in doc["claims"]:
            self.assertNotIn("999", c["article_claim"])
            self.assertNotIn("1999", c["article_claim"])

    def test_generic_prose_not_a_claim(self):
        md = _article_shell(
            "The broader market mood remained mixed as traders waited for clearer guidance."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        generics = [
            c
            for c in doc["claims"]
            if "broader market mood" in c["article_claim"].lower()
        ]
        self.assertEqual(generics, [])

    def test_summary_counts_match(self):
        md = _article_shell(
            "Robinhood has launched crypto trading in the UK via Bitstamp, "
            "giving investors access to over 50 digital assets including XRP. "
            "NexusTrust secretly acquired MoonPay for $3 billion in 2021 after a private auction."
        )
        doc = aae.audit_article(
            md,
            _ver(_rc("fact_001", self.research_exact)),
            validated=self.validated,
        )
        s = doc["summary"]
        self.assertEqual(s["article_claims"], len(doc["claims"]))
        self.assertEqual(
            s["supported"]
            + s["single_source_refs"]
            + s["unmapped"]
            + s["number_mismatches"]
            + s["year_mismatches"]
            + s["conflict_refs"],
            s["article_claims"],
        )
        self.assertTrue(aae.audit_ok(doc))

    def test_missing_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = os.path.join(tmp, "final.md")
            out = os.path.join(tmp, "article_audit.json")
            with open(art, "w", encoding="utf-8") as f:
                f.write(_article_shell(self.research_exact))
            code, doc = aae.run_audit(art, os.path.join(tmp, "missing.json"), out)
            self.assertEqual(code, 1)
            self.assertEqual(doc.get("status"), "error")

    def test_missing_consistency_handled_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = os.path.join(tmp, "final.md")
            ver = os.path.join(tmp, "verification.json")
            out = os.path.join(tmp, "article_audit.json")
            with open(art, "w", encoding="utf-8") as f:
                f.write(_article_shell(self.research_exact))
            with open(ver, "w", encoding="utf-8") as f:
                json.dump(_ver(_rc("fact_001", self.research_exact)), f)
            code, doc = aae.run_audit(
                art,
                ver,
                out,
                consistency_path=os.path.join(tmp, "no_consistency.json"),
            )
            self.assertEqual(code, 0)
            self.assertTrue(aae.audit_ok(doc))
            self.assertEqual(doc["summary"]["conflict_refs"], 0)

    def test_resume_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = os.path.join(tmp, "final.md")
            ver = os.path.join(tmp, "verification.json")
            out = os.path.join(tmp, "article_audit.json")
            with open(art, "w", encoding="utf-8") as f:
                f.write(_article_shell(self.research_exact))
            with open(ver, "w", encoding="utf-8") as f:
                json.dump(_ver(_rc("fact_001", self.research_exact)), f)
            code1, doc1 = aae.run_audit(art, ver, out)
            self.assertEqual(code1, 0)
            code2, doc2 = aae.run_audit(art, ver, out)
            self.assertEqual(code2, 0)
            self.assertEqual(doc2["summary"], doc1["summary"])


if __name__ == "__main__":
    unittest.main()
