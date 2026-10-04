from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.enrich import (
    STATUS_INSUFFICIENT,
    STATUS_SUFFICIENT,
    enrich_stories,
    evidence_sufficiency,
    extract_factual_snippets,
)
from newsagent_v2.article.input import build_article_input, evidence_text_blobs
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.grounding import check_body_claim_coverage, sentence_covered_by_claims
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.batch.completeness import account_requested_ids, require_complete_ids
from newsagent_v2.batch.contract import BatchError
from newsagent_v2.batch.runner import run_top5_batch
from tests.fixtures.article_qa import clean_article
from tests.test_article_qa import _codes

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "enrichment"
FAILED_BATCH = FIXTURE_DIR / "batch_20260914T142536Z-38a70731.json"
WTO_HTML = (FIXTURE_DIR / "wto_page.html").read_text(encoding="utf-8")
WTO_SECOND = (FIXTURE_DIR / "wto_second.html").read_text(encoding="utf-8")
LIVE_EVENT_023 = (
    REPO / "output" / "approval" / "20260914T142536Z-38a70731" / "stories" / "event-023.json"
)
IDS = [f"event-00{i}" for i in range(1, 6)]


def _thin_story(event_id: str, *, words: int = 16) -> dict:
    summary = " ".join(["brief"] * max(1, words - 4)) + " update."
    candidate = {
        "event_id": event_id,
        "representative_title": f"Headline {event_id}",
        "event_score": 1.0,
        "sources": ["Wire"],
        "source_count": 1,
        "evidence": [
            {
                "source": "Wire",
                "source_type": "newsroom",
                "source_role": "discovery",
                "source_authority": 0.5,
                "title": f"Headline {event_id}",
                "url": f"https://example.com/{event_id}",
                "published": "Mon, 14 Sep 2026 12:00:00 +0000",
                "summary": summary,
            }
        ],
    }
    return {
        "event_id": event_id,
        "article_input": build_article_input(candidate),
        "article_url": f"https://example.com/{event_id}",
        "source_count": 1,
    }


def _fetch(url: str):
    if url.endswith("event-019") or "wto" in url:
        html = WTO_HTML if "second" not in url else WTO_SECOND
        if url.endswith("second"):
            html = WTO_SECOND
        return 200, "text/html; charset=utf-8", html.encode("utf-8"), url
    if "example.com/event-019" in url:
        return 200, "text/html; charset=utf-8", WTO_HTML.encode("utf-8"), url
    if "example.com/second" in url:
        return 200, "text/html; charset=utf-8", WTO_SECOND.encode("utf-8"), url
    return 404, "text/plain", b"missing", url


class EvidenceEnrichmentTests(unittest.TestCase):
    def test_failed_batch_fixture_is_thin_and_incomplete(self) -> None:
        payload = json.loads(FAILED_BATCH.read_text(encoding="utf-8"))
        self.assertEqual(payload["image_request_count"], 0)
        self.assertEqual(payload["telegram_sends"], 0)
        self.assertEqual(payload["skip_reasons"]["event-003"], "missing_article")
        self.assertEqual(payload["skip_reasons"]["event-022"], "missing_article")
        self.assertLess(payload["evidence_word_counts"]["event-019"], 80)
        self.assertLess(payload["article_word_counts"]["event-019"], 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_thin_rss_evidence_is_insufficient_before_editorial(self) -> None:
        story = _thin_story("event-019", words=16)

        def empty_fetch(url: str):
            return 404, "text/plain", b"", url

        enriched = enrich_stories([story], fetch=empty_fetch, now="2026-09-14T14:25:36Z")[0]
        self.assertEqual(enriched["skip_reason"], STATUS_INSUFFICIENT)
        self.assertEqual(enriched["evidence_sufficiency"]["status"], STATUS_INSUFFICIENT)
        self.assertLess(enriched["evidence_sufficiency"]["distinct_fact_count"], 8)
        self.assertLess(enriched["evidence_sufficiency"]["extracted_evidence_words"], 350)

    def test_enrichment_strips_boilerplate_and_keeps_provenance(self) -> None:
        story = _thin_story("event-019", words=16)
        story["article_input"]["evidence"][0]["url"] = "https://example.com/event-019"
        story["article_input"]["evidence"].append(
            {
                "source": "SecondWire",
                "source_type": "newsroom",
                "source_role": "primary_evidence",
                "source_authority": 0.9,
                "title": "Second source",
                "url": "https://example.com/second",
                "published": "Mon, 14 Sep 2026 12:00:00 +0000",
                "summary": "Secondary summary of the same WTO remarks.",
            }
        )
        enriched = enrich_stories([story], fetch=_fetch, now="2026-09-14T14:25:36Z")[0]
        evidence = enriched["article_input"]["evidence"]
        texts = " ".join(str(row.get("extracted_text") or "") for row in evidence)
        self.assertNotRegex(texts.lower(), r"accept cookies")
        self.assertNotRegex(texts.lower(), r"subscribe to our newsletter")
        self.assertNotRegex(texts.lower(), r"related stories")
        row = evidence[0]
        self.assertEqual(row["retrieved_at_utc"], "2026-09-14T14:25:36Z")
        self.assertEqual(row["event_id"], "event-019")
        self.assertTrue(row["research_only"])
        self.assertTrue(row.get("extracted_text"))
        self.assertEqual(enriched["evidence_sufficiency"]["status"], STATUS_SUFFICIENT)
        self.assertGreaterEqual(enriched["evidence_sufficiency"]["distinct_fact_count"], 8)
        self.assertGreaterEqual(enriched["evidence_sufficiency"]["extracted_evidence_words"], 80)
        self.assertIn("entity_count", enriched["evidence_sufficiency"])
        self.assertIn("chronology_time_fact_count", enriched["evidence_sufficiency"])
        self.assertTrue(enriched["evidence_sufficiency"]["primary_source_present"])
        self.assertNotIn("skip_reason", enriched)

    def test_extracted_jsonld_and_paragraphs(self) -> None:
        extracted = extract_factual_snippets(WTO_HTML)
        blob = extracted["extracted_text"].lower()
        self.assertIn("3 percent", blob)
        self.assertNotIn("buy this token now", blob)
        self.assertEqual(extracted["research_only"], True)

    def test_meta_description_beats_nav_chrome(self) -> None:
        html = """
        <html><head>
          <title>Trump names Jay Clayton AI czar</title>
          <meta name="description" content="Trump names Jay Clayton AI czar to lead the Super Intelligence Force, a task force due to report on AI risks within 120 days." />
        </head><body>
          <div>Español Sections Bitcoin DeFi Ethereum NFTs AI Agents Regulation Web3 Business Ecosystem</div>
        </body></html>
        """
        extracted = extract_factual_snippets(html)
        self.assertEqual(extracted["extraction_method"], "meta_description")
        self.assertIn("jay clayton", extracted["extracted_text"].lower())
        self.assertNotIn("español sections", extracted["extracted_text"].lower())
        self.assertNotIn("bitcoin defi ethereum", extracted["extracted_text"].lower())

    def test_source_similarity_still_flags_copied_extracted_text(self) -> None:
        story = _thin_story("event-019", words=16)
        story["article_input"]["evidence"][0]["url"] = "https://example.com/event-019"
        enriched = enrich_stories([story], fetch=_fetch, now="2026-09-14T14:25:36Z")[0]
        article_input = enriched["article_input"]
        extracted = " ".join(evidence_text_blobs(article_input))
        article = clean_article()
        article["article_body"] = extracted + " " + extracted
        result = run_article_qa(article, article_input)
        self.assertFalse(result["publishable"])
        codes = _codes(result, severity="critical")
        self.assertTrue(
            "exact_phrase_overlap" in codes or "high_sentence_similarity" in codes
        )

    def test_no_image_or_telegram_before_qa_pass(self) -> None:
        stories = []
        for event_id in IDS:
            row = _thin_story(event_id, words=16)
            row["skip_reason"] = STATUS_INSUFFICIENT
            stories.append(row)
        images = []
        telegrams = []

        def image_fn(job):
            images.append(job["event_id"])
            return {"success": True, "final_path": "unused.png"}

        batch = run_top5_batch(
            event_ids=IDS,
            stories=stories,
            image_fn=image_fn,
            parallel_images=False,
        )
        self.assertEqual(images, [])
        self.assertEqual(batch["telemetry"]["image_request_count"], 0)
        self.assertEqual(batch["telemetry"]["telegram_sends"], 0)
        self.assertTrue(all(row["skip_reason"] == STATUS_INSUFFICIENT for row in batch["stories"]))
        self.assertFalse(any(row.get("deliverable") for row in batch["stories"]))
        self.assertEqual(telegrams, [])


class BatchCompletenessTests(unittest.TestCase):
    def test_missing_ids_are_detected(self) -> None:
        requested = ["event-019", "event-023", "event-002", "event-003", "event-022"]
        returned = {"event-019": {}, "event-023": {}, "event-002": {}}
        failed: dict[str, str] = {}
        report = account_requested_ids(requested, returned, failed)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_ids"], ["event-003", "event-022"])
        with self.assertRaises(BatchError) as ctx:
            require_complete_ids(requested, returned, failed)
        self.assertEqual(ctx.exception.code, "silently_missing_event_ids")

    def test_explicit_failures_account_for_requested_ids(self) -> None:
        requested = ["event-019", "event-023", "event-002", "event-003", "event-022"]
        returned = {"event-019": {"headline": "a"}, "event-023": {"headline": "b"}}
        failed = {
            "event-002": STATUS_INSUFFICIENT,
            "event-003": "article_generation_failed",
            "event-022": "article_generation_failed",
        }
        report = require_complete_ids(requested, returned, failed)
        self.assertTrue(report["ok"])

    def test_duplicates_and_unknown_ids_rejected(self) -> None:
        with self.assertRaises(BatchError):
            require_complete_ids(["a", "a", "b", "c", "d"], {"a": {}}, {"b": "x", "c": "x", "d": "x"})
        with self.assertRaises(BatchError):
            require_complete_ids(
                ["a", "b", "c", "d", "e"],
                {"a": {}, "ghost": {}},
                {"b": "x", "c": "x", "d": "x", "e": "x"},
            )


class CompoundClaimTests(unittest.TestCase):
    def test_union_of_two_claims_covers_compound_sentence(self) -> None:
        sentence = (
            "According to blockchain security firm Blockaid, the attack resulted "
            "in the minting of roughly 46.1 billion syBTC, but the attacker only "
            "realized about $336,000 in proceeds."
        )
        claims = [
            "Blockaid reported that roughly 46.1 billion syBTC were minted during the exploit.",
            "Blockaid estimated the attacker realized about $336,000 in proceeds.",
        ]
        self.assertTrue(sentence_covered_by_claims(sentence, claims))
        article = {
            "article_body": sentence,
            "claims": [
                {"claim_id": "c1", "text": claims[0], "claim_type": "number", "evidence_refs": []},
                {"claim_id": "c2", "text": claims[1], "claim_type": "number", "evidence_refs": []},
            ],
        }
        issues, metrics = check_body_claim_coverage(article)
        self.assertEqual(metrics["uncovered_assertive_sentence_count"], 0)
        self.assertFalse(any(item["code"] == "body_assertion_not_in_claims" for item in issues))

    def test_extra_unsupported_clause_still_fails(self) -> None:
        sentence = (
            "According to blockchain security firm Blockaid, the attack resulted "
            "in the minting of roughly 46.1 billion syBTC, but the attacker only "
            "realized about $336,000 in proceeds, while the CEO resigned the same day."
        )
        claims = [
            "Blockaid reported that roughly 46.1 billion syBTC were minted during the exploit.",
            "Blockaid estimated the attacker realized about $336,000 in proceeds.",
        ]
        self.assertFalse(sentence_covered_by_claims(sentence, claims))
        article = {
            "article_body": sentence,
            "claims": [
                {"claim_id": "c1", "text": claims[0], "claim_type": "number", "evidence_refs": []},
                {"claim_id": "c2", "text": claims[1], "claim_type": "number", "evidence_refs": []},
            ],
        }
        issues, _metrics = check_body_claim_coverage(article)
        self.assertTrue(any(item["code"] == "body_assertion_not_in_claims" for item in issues))


class Utf8DiagnosisTests(unittest.TestCase):
    def test_narrow_nbsp_is_valid_utf8_not_mojibake(self) -> None:
        snippet = "roughly 46.1\u202fbillion syBTC"
        raw = snippet.encode("utf-8")
        self.assertEqual(raw.decode("utf-8"), snippet)
        self.assertIn(b"\xe2\x80\xaf", raw)
        self.assertNotIn("Ã¢â‚¬Â¯", snippet)
        misread = raw.decode("cp1252")
        self.assertIn("Ã¢", misread)
        roundtrip = json.loads(json.dumps({"text": snippet}, ensure_ascii=False))
        self.assertEqual(roundtrip["text"], snippet)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utf8.json"
            path.write_text(json.dumps({"text": snippet}, ensure_ascii=False), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["text"], snippet)
            self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_live_event_023_file_is_utf8(self) -> None:
        if not LIVE_EVENT_023.is_file():
            self.skipTest("live approval artifact not present")
        raw = LIVE_EVENT_023.read_bytes()
        text = raw.decode("utf-8")
        self.assertNotIn("\ufffd", text)
        if "46.1" in text:
            idx = text.index("46.1")
            window = text[idx : idx + 12]
            self.assertNotIn("Ã¢â‚¬Â¯", window)


if __name__ == "__main__":
    unittest.main()


