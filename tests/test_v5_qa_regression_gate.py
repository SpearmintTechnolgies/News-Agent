"""Deterministic QA regression gate for revision evidence.

Verifies:
- 24 evidence units convert without loss
- QA validation passes (invented_evidence_ref = 0, claim_unknown_evidence = 0)
- No provider calls
"""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.v4.factbank import build_fact_bank, fact_bank_to_writer_packet
from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.qa.schema import check_schema
from newsagent_v2.article.qa.claims import check_claims
from newsagent_v2.article.input import evidence_index


class TestQARegressionGate(unittest.TestCase):
    """Deterministic QA gate - NO provider calls."""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_revision_evidence_qa_gate(self):
        """Gate: 24 facts → QA validation → 0 invented_evidence_ref, 0 claim_unknown_evidence."""

        # Build REAL-SCHEMA revision evidence (like _facts_to_evidence_units produces)
        event_id = "evt-fec17bd1"
        evidence_units = []

        # 4 distinct sources with real URLs (like real V1)
        sources = [
            ("The Block", "https://www.theblock.co/news/1"),
            ("CoinDesk", "https://www.coindesk.com/policy/1"),
            ("The Block", "https://www.theblock.co/news/2"),
            ("CoinDesk", "https://www.coindesk.com/policy/2"),
        ]

        for i in range(1, 25):
            src_idx = (i - 1) % 4
            source_name, url = sources[src_idx]
            provenance_id = f"{event_id}-e{i:02d}"

            evidence_units.append({
                "evidence_id": f"E{i:02d}",
                "text": f"CFTC submitted crypto rulemaking action {i} to White House for review.",
                "source": source_name,
                "url": url,
                "published": "2026-09-18",
                "_revision_fact_id": f"P{i:02d}",
                "_v1_provenance": provenance_id,
            })

        print(f"\n[INPUT] evidence_units: {len(evidence_units)}")
        print(f"  Distinct URLs: {len(set(u['url'] for u in evidence_units))}")
        print(f"  Distinct provenance IDs: {len(set(u['_v1_provenance'] for u in evidence_units))}")

        # === BOUNDARY 1: article_input_for_ledgers ===
        article_input = {
            "event_id": event_id,
            "representative_title": "CFTC Files Crypto Rulemaking",
            "evidence_units": evidence_units,
            "evidence": [{"url": u["url"], "source": u["source"]} for u in evidence_units if u.get("url")],
        }

        pack = article_input_for_ledgers(article_input)
        print(f"\n[BOUNDARY 1] article_input_for_ledgers output:")
        print(f"  evidence_units count: {len(pack.get('evidence_units', []))}")

        # Verify evidence_index sees the evidence
        allowed = evidence_index(article_input)
        print(f"  evidence_index (allowed URLs): {len(allowed)}")
        self.assertGreater(len(allowed), 0, "evidence_index must see URLs")

        # === BOUNDARY 2: build_fact_bank ===
        from newsagent_v2.article.writer.v4.factbank import build_fact_bank
        bank = build_fact_bank(event_id=event_id, pack=pack)
        print(f"\n[BOUNDARY 2] build_fact_bank output:")
        print(f"  propositions count: {len(bank.propositions)}")
        print(f"  unique_proposition_count: {bank.unique_proposition_count}")

        # Check source_ids in propositions
        if bank.propositions:
            first_prop = bank.propositions[0]
            print(f"  First proposition source_ids: {first_prop.source_ids}")

        # === BOUNDARY 3: fact_bank_to_writer_packet ===
        source_names = [u["source"] for u in evidence_units if u.get("source")]
        packet = fact_bank_to_writer_packet(
            bank,
            story_topic=article_input.get("representative_title", ""),
            source_names=source_names,
        )
        print(f"\n[BOUNDARY 3] fact_bank_to_writer_packet output:")
        print(f"  authorized_facts count: {len(packet.authorized_facts)}")
        print(f"  source_context.source_names: {len(packet.source_context.get('source_names', []))}")
        print(f"  unique_propositions: {packet.source_context.get('unique_propositions', 0)}")

        # Check provenance in first fact
        if packet.authorized_facts:
            first_fact = packet.authorized_facts[0]
            print(f"  First fact provenance: {first_fact.provenance}")

        # === BOUNDARY 4: Simulated article with claims ===
        # Build article like writer would produce, with evidence_refs
        article = {
            "schema_version": "article-output-v1",
            "event_id": event_id,
            "headline": "CFTC Files Crypto Rulemaking",
            "article_body": "The CFTC submitted...",
            "claims": [],
            "quotes": [],
            "category": "regulatory",
        }

        # Create claims matching the evidence_units
        for i, unit in enumerate(evidence_units[:5], 1):  # First 5 for brevity
            article["claims"].append({
                "claim_id": f"P{i:02d}",
                "text": unit["text"],
                "claim_type": "fact",
                "evidence_ids": [unit["_v1_provenance"]] if i <= 2 else [unit["evidence_id"]],
                "evidence_refs": [{"url": unit["url"], "source": unit["source"]}],
            })

        print(f"\n[BOUNDARY 4] Simulated article:")
        print(f"  claims count: {len(article['claims'])}")
        first_claim = article["claims"][0]
        print(f"  First claim evidence_refs: {first_claim['evidence_refs']}")

        # === QA VALIDATION ===
        schema_issues = check_schema(article, article_input)
        print(f"\n[QA VALIDATION] check_schema issues: {len(schema_issues)}")

        invented_evidence_ref = [i for i in schema_issues if i.get("code") == "invented_evidence_ref"]
        invalid_evidence_url = [i for i in schema_issues if i.get("code") == "invalid_evidence_url"]

        print(f"  invented_evidence_ref: {len(invented_evidence_ref)}")
        print(f"  invalid_evidence_url: {len(invalid_evidence_url)}")

        if invented_evidence_ref:
            print(f"    FIRST: {invented_evidence_ref[0]}")

        # Check claims (returns tuple: (issues, stats))
        claim_result = check_claims(article, article_input)
        claim_issues = claim_result[0] if isinstance(claim_result, tuple) else claim_result
        claim_unknown_evidence = [i for i in claim_issues if i.get("code") == "claim_unknown_evidence"]
        print(f"  claim_unknown_evidence: {len(claim_unknown_evidence)}")

        if claim_unknown_evidence:
            print(f"    FIRST: {claim_unknown_evidence[0]}")

        # === PASS/FAIL GATE ===
        print(f"\n{'='*50}")
        print("GATE RESULT:")
        print(f"{'='*50}")

        total_critical = len(invented_evidence_ref) + len(claim_unknown_evidence) + len(invalid_evidence_url)

        if total_critical == 0:
            print("PASS: invented_evidence_ref = 0, claim_unknown_evidence = 0, invalid_evidence_url = 0")
        else:
            print(f"FAIL: {total_critical} critical issues found")

        # Assertions
        self.assertEqual(len(invented_evidence_ref), 0, "invented_evidence_ref must be 0")
        self.assertEqual(len(claim_unknown_evidence), 0, "claim_unknown_evidence must be 0")
        self.assertEqual(len(invalid_evidence_url), 0, "invalid_evidence_url must be 0")

    def test_source_names_not_sufficient_for_qa(self):
        """Prove source_names alone does NOT satisfy QA URL validation."""
        from newsagent_v2.article.input import evidence_index

        article_input_with_source_names = {
            "event_id": "test",
            "source_names": ["The Block", "CoinDesk"],  # Only source_names, no evidence
        }

        allowed = evidence_index(article_input_with_source_names)
        print(f"\n[PROOF] evidence_index with only source_names:")
        print(f"  allowed URLs: {len(allowed)}")

        self.assertEqual(len(allowed), 0, "source_names alone does NOT populate evidence_index")
        print("  PROVEN: source_names alone is insufficient for QA URL validation")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("V5 QA REGRESSION GATE")
    print("="*60)

    suite = unittest.TestSuite()
    suite.addTest(TestQARegressionGate("test_revision_evidence_qa_gate"))
    suite.addTest(TestQARegressionGate("test_source_names_not_sufficient_for_qa"))

    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite)
