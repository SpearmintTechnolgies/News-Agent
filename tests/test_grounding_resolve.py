from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.grounding import check_body_claim_coverage, sentence_covered_by_claims
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import split_sentences
from newsagent_v2.article.writer.grounding_resolve import (
    RESOLVED_COVERAGE_KEY,
    resolve_grounding,
    safe_quote_coverage_texts,
)
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID, SOURCE_BATCH_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.freeze import sha256_payload

REPO = Path(__file__).resolve().parents[1]
RUN_ARTICLE = (
    REPO
    / "benchmarks"
    / "writer_bakeoff"
    / EVENT_ID
    / "live_runs"
    / "20260915T092107Z"
    / "gemini_3_6_flash_article_first"
    / "article.json"
)
FIXTURE = REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID

SWAYING = (
    "The release follows an extended legislative process aimed at swaying "
    "Senate Democrats to support advancing the digital asset measure."
)
ENDORSEMENT = (
    "A central element of the updated CLARITY Act text involves ethics rules "
    "that have received the explicit endorsement of US President Donald Trump."
)
STABLECOIN = (
    "Additionally, the revised bill establishes regulatory provisions governing "
    "stablecoin yield, addressing critical components of cryptocurrency oversight "
    "under discussion in Congress."
)
QUOTE = (
    '"After a year of intense daily bipartisan negotiations, this bill is ready," '
    "Lummis stated."
)
TIME_JOINED = (
    "The upcoming procedural vote is scheduled for Tuesday at 2:15 p.m. "
    "Eastern Time, coming just two days after the text's release, to determine "
    "whether the Senate will advance the bill toward full floor consideration."
)


def _load_run():
    article = json.loads(RUN_ARTICLE.read_text(encoding="utf-8"))
    fixture = load_fixture(FIXTURE)
    return article, fixture["article_input"]


class GroundingResolverTests(unittest.TestCase):
    def test_pm_abbreviation_does_not_split_sentences(self) -> None:
        sentences = split_sentences(TIME_JOINED)
        self.assertEqual(len(sentences), 1)
        self.assertIn("2:15 p.m. Eastern Time", sentences[0])
        et = (
            "The procedural vote scheduled for Tuesday at 2:15 p.m. ET marks a "
            "decisive juncture for the CLARITY Act."
        )
        self.assertEqual(split_sentences(et), [et])

    def test_ordinary_sentences_still_split(self) -> None:
        text = "First fact is stated. Second fact follows."
        self.assertEqual(split_sentences(text), ["First fact is stated.", "Second fact follows."])

    def test_exact_quote_recovery_uses_existing_ledger(self) -> None:
        article, article_input = _load_run()
        texts = [str(c["text"]) for c in article["claims"]]
        self.assertFalse(sentence_covered_by_claims(QUOTE, texts))
        extras = safe_quote_coverage_texts(article, article_input)
        self.assertTrue(any("this bill is ready" in row for row in extras))
        self.assertTrue(sentence_covered_by_claims(QUOTE, texts + extras))

    def test_exact_date_time_recovery_after_split_fix(self) -> None:
        article, _article_input = _load_run()
        texts = [str(c["text"]) for c in article["claims"]]
        sentences = split_sentences(article["article_body"])
        self.assertIn(TIME_JOINED, sentences)
        self.assertTrue(sentence_covered_by_claims(TIME_JOINED, texts))
        self.assertFalse(any(s == "ET marks a decisive juncture for the CLARITY Act." for s in sentences))

    def test_unsupported_motive_remains_rejected(self) -> None:
        article, article_input = _load_run()
        issues, metrics = check_body_claim_coverage(article, article_input)
        uncovered = metrics["uncovered_assertive_sentences"]
        self.assertIn(SWAYING, uncovered)
        self.assertTrue(any(item["code"] == "body_assertion_not_in_claims" and item.get("sentence") == SWAYING for item in issues))

    def test_semantic_strengthening_remains_rejected(self) -> None:
        article, article_input = _load_run()
        uncovered = check_body_claim_coverage(article, article_input)[1]["uncovered_assertive_sentences"]
        self.assertIn(ENDORSEMENT, uncovered)

    def test_partial_evidence_does_not_become_full_support(self) -> None:
        article, article_input = _load_run()
        uncovered = check_body_claim_coverage(article, article_input)[1]["uncovered_assertive_sentences"]
        self.assertIn(STABLECOIN, uncovered)

    def test_unknown_evidence_quote_rejected(self) -> None:
        article, article_input = _load_run()
        article = deepcopy(article)
        article["quotes"] = [
            {
                "text": "This invented quote is not recoverable.",
                "kind": "direct",
                "attribution": "Someone",
                "evidence_ids": ["event-005-e99"],
            }
        ]
        self.assertEqual(safe_quote_coverage_texts(article, article_input), [])

    def test_foreign_evidence_quote_rejected(self) -> None:
        article, article_input = _load_run()
        article = deepcopy(article)
        article["quotes"] = [
            {
                "text": article["quotes"][0]["text"],
                "kind": "direct",
                "attribution": "Cynthia Lummis",
                "evidence_ids": ["event-039-e01"],
            }
        ]
        other = {
            "event_id": "event-039",
            "evidence_units": [{"evidence_id": "event-039-e01", "url": "https://example.com/other", "text": "x"}],
        }
        self.assertEqual(
            safe_quote_coverage_texts(article, article_input, other_article_inputs=[other]),
            [],
        )

    def test_resolver_does_not_mutate_article_or_invent_claims(self) -> None:
        article, article_input = _load_run()
        original_body = article["article_body"]
        original_claims = json.dumps(article["claims"])
        resolved = resolve_grounding(article, article_input)
        self.assertEqual(article["article_body"], original_body)
        self.assertEqual(json.dumps(article["claims"]), original_claims)
        self.assertEqual(resolved["article_body"], original_body)
        self.assertEqual(json.dumps(resolved["claims"]), original_claims)
        self.assertIn(RESOLVED_COVERAGE_KEY, resolved)
        self.assertNotIn(RESOLVED_COVERAGE_KEY, article)
        self.assertTrue(any("this bill is ready" in row for row in resolved[RESOLVED_COVERAGE_KEY]))

    def test_qa_thresholds_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)

    def test_persisted_article_and_batch_untouched(self) -> None:
        article, article_input = _load_run()
        fixture = load_fixture(FIXTURE)
        self.assertEqual(fixture["manifest"]["source_batch_id"], SOURCE_BATCH_ID)
        self.assertEqual(
            sha256_payload(article_input),
            fixture["manifest"]["hashes"]["article_input_sha256"],
        )
        self.assertEqual(article["headline"], json.loads(RUN_ARTICLE.read_text(encoding="utf-8"))["headline"])

    def test_safe_recovery_does_not_make_existing_article_publishable(self) -> None:
        article, article_input = _load_run()
        result = run_article_qa(deepcopy(article), article_input, article_mode="normal")
        self.assertFalse(result["publishable"])
        uncovered = result["metrics"]["uncovered_assertive_sentences"]
        self.assertIn(SWAYING, uncovered)
        self.assertIn(ENDORSEMENT, uncovered)
        self.assertIn(STABLECOIN, uncovered)
        self.assertNotIn(QUOTE, uncovered)
        codes = [item["code"] for item in result["critical_failures"]]
        self.assertIn("exact_phrase_overlap", codes)
        self.assertIn("body_assertion_not_in_claims", codes)


if __name__ == "__main__":
    unittest.main()


