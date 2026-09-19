from __future__ import annotations

import unittest

from newsagent_v2.article.contract import (
    ARTICLE_OUTPUT_SCHEMA_VERSION,
    FUTURE_QA_HOOKS,
)
from newsagent_v2.article.input import build_article_input
from newsagent_v2.article.qa import run_article_qa
from tests.fixtures.article_qa import (
    CANDIDATE,
    JUDGMENT,
    all_caps_headline,
    article_input,
    bad_slug,
    claim_without_evidence,
    clean_article,
    clean_paraphrase_with_entity_overlap,
    copied_source_paragraph,
    direct_quote_without_attribution,
    invented_evidence_ref,
    localhost_leak,
    malformed_punctuation,
    repeated_paragraph,
    unsupported_numeric_headline,
    windows_path_leak,
)


def _codes(result: dict, *, severity: str | None = None) -> set[str]:
    bucket = result["critical_failures"] + result["warnings"]
    if severity == "critical":
        bucket = result["critical_failures"]
    if severity == "warning":
        bucket = result["warnings"]
    return {item["code"] for item in bucket}


class ArticleInputTests(unittest.TestCase):
    def test_builder_copies_evidence_without_invention(self) -> None:
        payload = build_article_input(CANDIDATE, JUDGMENT)
        self.assertEqual(payload["event_id"], "event-syn-001")
        self.assertEqual(len(payload["evidence"]), 1)
        self.assertEqual(payload["evidence"][0]["url"], CANDIDATE["evidence"][0]["url"])
        self.assertFalse(payload["browse"])
        self.assertFalse(payload["fetch_fulltext"])
        self.assertEqual(payload["is_current_event"], True)
        self.assertEqual(payload["category"], "security_incident")


class ArticleQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = article_input()

    def test_clean_article_passes(self) -> None:
        result = run_article_qa(clean_article(), self.pack)
        self.assertTrue(result["qa_passed"])
        self.assertEqual(result["critical_failures"], [])
        self.assertGreater(result["metrics"]["article_word_count"], 20)
        self.assertGreater(result["metrics"]["headline_word_count"], 2)
        self.assertGreaterEqual(result["metrics"]["claim_count"], 8)
        self.assertEqual(
            result["metrics"]["claims_with_evidence"],
            result["metrics"]["claim_count"],
        )
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 350)

    def test_unsupported_numeric_headline(self) -> None:
        result = run_article_qa(unsupported_numeric_headline(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertIn("headline_unsupported_number", _codes(result, severity="critical"))

    def test_factual_claim_without_evidence(self) -> None:
        result = run_article_qa(claim_without_evidence(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertTrue(
            {"claim_missing_evidence", "numeric_claim_no_evidence"}
            & _codes(result, severity="critical")
        )

    def test_invented_evidence_ref(self) -> None:
        result = run_article_qa(invented_evidence_ref(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertTrue(
            {"invented_evidence_ref", "claim_unknown_evidence"}
            & _codes(result, severity="critical")
        )

    def test_copied_source_paragraph(self) -> None:
        result = run_article_qa(copied_source_paragraph(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertTrue(
            {"exact_phrase_overlap", "high_sentence_similarity", "repeated_paragraph"}
            & _codes(result, severity="critical")
        )
        self.assertGreater(result["metrics"]["max_similarity"], 0.5)

    def test_repeated_paragraph(self) -> None:
        result = run_article_qa(repeated_paragraph(), self.pack)
        # Stylistic repetition is WARNING â€” not article-killing alone.
        self.assertIn("repeated_paragraph", _codes(result, severity="warning"))

    def test_malformed_punctuation(self) -> None:
        result = run_article_qa(malformed_punctuation(), self.pack)
        self.assertIn("malformed_punctuation", _codes(result, severity="warning"))

    def test_all_caps_headline(self) -> None:
        result = run_article_qa(all_caps_headline(), self.pack)
        self.assertIn("headline_all_caps", _codes(result, severity="warning"))

    def test_direct_quote_without_attribution(self) -> None:
        result = run_article_qa(direct_quote_without_attribution(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertIn("direct_quote_no_attribution", _codes(result, severity="critical"))

    def test_bad_slug(self) -> None:
        result = run_article_qa(bad_slug(), self.pack)
        # SEO-only defects are WARNING and must not alone prevent publication.
        self.assertIn("invalid_slug", _codes(result, severity="warning"))
        # May still fail for other reasons in fixture; slug alone is not critical.
        self.assertNotIn("invalid_slug", _codes(result, severity="critical"))

    def test_windows_path_leak(self) -> None:
        result = run_article_qa(windows_path_leak(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertIn("windows_path_leak", _codes(result, severity="critical"))

    def test_localhost_leak(self) -> None:
        result = run_article_qa(localhost_leak(), self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertIn("localhost_leak", _codes(result, severity="critical"))

    def test_clean_paraphrase_allows_entity_and_number_overlap(self) -> None:
        result = run_article_qa(clean_paraphrase_with_entity_overlap(), self.pack)
        self.assertTrue(result["qa_passed"])
        self.assertNotIn("exact_phrase_overlap", _codes(result, severity="critical"))
        self.assertNotIn("high_sentence_similarity", _codes(result, severity="critical"))

    def test_malformed_article_object(self) -> None:
        result = run_article_qa(["not", "an", "object"], self.pack)
        self.assertFalse(result["qa_passed"])
        self.assertIn("schema_not_object", _codes(result, severity="critical"))

    def test_schema_version_constant(self) -> None:
        self.assertEqual(clean_article()["schema_version"], ARTICLE_OUTPUT_SCHEMA_VERSION)
        self.assertIn("semantic_claim_entailment", FUTURE_QA_HOOKS)

    def test_critical_means_not_publishable(self) -> None:
        result = run_article_qa(windows_path_leak(), self.pack)
        self.assertFalse(result["publishable"])
        self.assertGreaterEqual(result.get("critical_count", 1), 1)
        clean = run_article_qa(clean_article(), self.pack)
        self.assertTrue(clean["publishable"])
        self.assertEqual(clean.get("critical_count"), 0)


if __name__ == "__main__":
    unittest.main()


