"""TEMPORARY / DEMO article minimum (ARTICLE_MIN_WORDS). Other QA gates unchanged."""

from __future__ import annotations

import os
import unittest
from copy import deepcopy

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import (
    DEMO_ARTICLE_MIN_WORDS_ACTIVE_MARKER,
    NORMAL_ARTICLE_POLICY,
    PRODUCTION_HARD_MINIMUM_WORDS,
    active_normal_depth_policy,
    resolve_article_hard_minimum_words,
)
from newsagent_v2.article.qa.textutil import word_count
from tests.fixtures.article_qa import article_input, clean_article, copied_source_paragraph


def _article_with_words(n: int) -> dict:
    article = clean_article()
    tokens = (
        "Northwind Payments reported that customer records were exposed after "
        "a spoofed government-domain email request reached staff. Investigators "
        "confirmed identity documents and payment history were involved. "
        "Security staff began notifying affected users after the disclosure. "
        "The company opened an internal review of how the fake domain request "
        "was processed by mail handlers. Retail payments customer files were "
        "the dataset described in the TestWire account of the incident."
    ).split()
    words: list[str] = []
    i = 0
    while len(words) < n - 1:
        words.append(tokens[i % len(tokens)])
        i += 1
    words.append("confirmed.")
    body = " ".join(words)
    assert word_count(body) == n, word_count(body)
    article["article_body"] = body
    return article


class DemoArticleMinWordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = article_input()
        self._prev = {
            key: os.environ.get(key)
            for key in ("ARTICLE_MIN_WORDS", "NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS")
        }

    def tearDown(self) -> None:
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_production_constant_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(PRODUCTION_HARD_MINIMUM_WORDS, 350)

    def test_119_words_fails_length_under_demo_120(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        os.environ.pop("NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS", None)
        policy = active_normal_depth_policy()
        self.assertEqual(policy.hard_minimum_words, 120)
        self.assertTrue(policy.temporary_demo_minimum)
        result = run_article_qa(_article_with_words(119), self.pack, depth_policy=policy)
        codes = {item["code"] for item in result["critical_failures"]}
        self.assertIn("below_article_minimum_length", codes)
        self.assertFalse(result["qa_passed"])

    def test_120_words_otherwise_clean_length_passes(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        policy = active_normal_depth_policy()
        result = run_article_qa(_article_with_words(120), self.pack, depth_policy=policy)
        codes = {item["code"] for item in result["critical_failures"]}
        self.assertNotIn("below_article_minimum_length", codes)
        self.assertEqual(result["metrics"]["depth_hard_minimum_words"], 120)
        self.assertTrue(result["metrics"].get(DEMO_ARTICLE_MIN_WORDS_ACTIVE_MARKER))
        # Other factual/safety gates must still be able to pass.
        self.assertTrue(result["qa_passed"] or "below_article_minimum_length" not in codes)

    def test_120_words_copyright_exact_overlap_still_fails(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        policy = active_normal_depth_policy()
        article = copied_source_paragraph()
        # Ensure length is at least 120 so length is not the failing gate.
        body = str(article.get("article_body") or "")
        if word_count(body) < 120:
            article["article_body"] = _pad_body_to_words(body + " " + body, 120)
        result = run_article_qa(article, self.pack, depth_policy=policy)
        codes = {item["code"] for item in result["critical_failures"]}
        self.assertTrue(
            {"exact_phrase_overlap", "high_sentence_similarity"} & codes,
            codes,
        )
        self.assertFalse(result["qa_passed"])

    def test_120_words_high_similarity_still_fails(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        policy = active_normal_depth_policy()
        article = copied_source_paragraph()
        body = str(article.get("article_body") or "")
        if word_count(body) < 120:
            article["article_body"] = _pad_body_to_words(body + " " + body, 130)
        result = run_article_qa(article, self.pack, depth_policy=policy)
        codes = {item["code"] for item in result["critical_failures"]}
        self.assertIn("high_sentence_similarity", codes)
        self.assertFalse(result["qa_passed"])

    def test_120_words_grounding_failure_still_fails(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        policy = active_normal_depth_policy()
        article = _article_with_words(120)
        article["article_body"] += (
            " Regulators in three countries opened a criminal inquiry on Tuesday."
        )
        result = run_article_qa(article, self.pack, depth_policy=policy)
        codes = {item["code"] for item in result["critical_failures"]}
        self.assertIn("body_assertion_not_in_claims", codes)
        self.assertNotIn("below_article_minimum_length", codes)
        self.assertFalse(result["qa_passed"])

    def test_120_words_unsupported_assertion_still_fails(self) -> None:
        os.environ["ARTICLE_MIN_WORDS"] = "120"
        policy = active_normal_depth_policy()
        article = _article_with_words(120)
        article["article_body"] += (
            " A separate offshore affiliate quietly moved customer deposits overnight."
        )
        result = run_article_qa(article, self.pack, depth_policy=policy)
        codes = {item["code"] for item in result["critical_failures"]}
        self.assertIn("body_assertion_not_in_claims", codes)
        self.assertNotIn("below_article_minimum_length", codes)
        self.assertFalse(result["qa_passed"])

    def test_resolve_reads_demo_env(self) -> None:
        os.environ.pop("ARTICLE_MIN_WORDS", None)
        os.environ["NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS"] = "120"
        floor, demo = resolve_article_hard_minimum_words()
        self.assertEqual(floor, 120)
        self.assertTrue(demo)


if __name__ == "__main__":
    unittest.main()


