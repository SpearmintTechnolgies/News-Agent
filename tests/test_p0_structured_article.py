from __future__ import annotations

import inspect
import json
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.batch_prompt import BATCH_SYSTEM_PROMPT
from newsagent_v2.article.batch_runner import generate_make_articles, normalize_batch_output
from newsagent_v2.article.enrich import MIN_DISTINCT_FACTS, MIN_EXTRACTED_WORDS
from newsagent_v2.article.prompt import SYSTEM_PROMPT, article_output_json_schema
from newsagent_v2.article.prompt_evidence import GROQ_BATCH_ADMISSION_TARGET, GROQ_ON_DEMAND_TPM_LIMIT
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY, WARN_SENTENCE_SIMILARITY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.render import materialize_article, render_article_body
from newsagent_v2.batch.viability import DEFAULT_CANDIDATE_SCAN_LIMIT
from newsagent_v2.control import live as live_mod
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS, build_top5_article_batch_request_body
from newsagent_v2.providers.groq_editorial import safe_chat_request_diagnostics
from tests.fixtures.article_qa import (
    EVIDENCE_URL,
    article_input,
    attach_article_sections,
    clean_article,
    structured_exploit_article,
    structured_regulatory_article,
)
from tests.test_article_qa import _codes
from tests.test_groq_editorial_benchmark import FakeResponse
from tests.test_p0_tpm_compaction import _load_make5_stories
from tests.test_top5_single_call import ENV, RecordingPost, _article, _payload, _sufficient_row

REPO = Path(__file__).resolve().parents[1]
MAKE6 = REPO / "output" / "approval" / "20260915T065609Z-30b3ccc3" / "stories"
BATCH_RUNNER_SRC = (REPO / "src" / "newsagent_v2" / "article" / "batch_runner.py").read_text(
    encoding="utf-8"
)
LIVE_SRC = inspect.getsource(live_mod)
MAKE6_IDS = ["event-005", "event-020", "event-028", "event-032"]


def _load_make6(event_id: str) -> dict:
    return json.loads((MAKE6 / f"{event_id}.json").read_text(encoding="utf-8"))


def _short_structured() -> dict:
    article = attach_article_sections(clean_article(), section_count=5)
    for section in article["article_sections"]:
        section["paragraphs"][0]["text"] = "Northwind Payments said 12,400 records were exposed."
    materialize_article(article)
    return article


class RendererTests(unittest.TestCase):
    def test_python_renders_article_body_from_sections(self) -> None:
        article = structured_regulatory_article()
        rendered = render_article_body(article)
        self.assertEqual(article["article_body"], rendered)
        self.assertGreaterEqual(word_count(rendered), 450)
        self.assertIn("\n\n", rendered)

    def test_rendered_article_at_least_350_passes_when_grounded(self) -> None:
        result = run_article_qa(structured_regulatory_article(), article_input())
        self.assertTrue(result["publishable"])
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 350)

    def test_rendered_article_below_350_fails(self) -> None:
        article = _short_structured()
        self.assertLess(word_count(article["article_body"]), 350)
        result = run_article_qa(article, article_input())
        self.assertFalse(result["publishable"])
        self.assertIn("below_article_minimum_length", _codes(result, severity="critical"))


class StructureContractTests(unittest.TestCase):
    def test_body_unit_missing_claim_ids_fails(self) -> None:
        article = structured_regulatory_article()
        article["article_sections"][0]["paragraphs"][0]["claim_ids"] = []
        result = run_article_qa(article, article_input())
        self.assertIn("paragraph_missing_claim_ids", _codes(result, severity="critical"))

    def test_unknown_claim_id_fails(self) -> None:
        article = structured_regulatory_article()
        article["article_sections"][0]["paragraphs"][0]["claim_ids"] = ["missing-claim"]
        result = run_article_qa(article, article_input())
        self.assertIn("unknown_claim_id", _codes(result, severity="critical"))

    def test_claim_without_evidence_refs_fails(self) -> None:
        article = structured_regulatory_article()
        article["claims"][0]["evidence_refs"] = []
        result = run_article_qa(article, article_input())
        self.assertIn("claim_missing_evidence", _codes(result, severity="critical"))

    def test_unknown_evidence_ref_fails(self) -> None:
        article = structured_regulatory_article()
        article["claims"][0]["evidence_refs"] = [{"url": "https://invented.example/x", "source": "Ghost"}]
        result = run_article_qa(article, article_input())
        self.assertTrue(
            {"unknown_evidence_ref", "claim_unknown_evidence"}
            & _codes(result, severity="critical")
        )

    def test_foreign_story_evidence_ref_fails(self) -> None:
        article = structured_regulatory_article()
        foreign_url = "https://example.com/other-event"
        article["claims"][0]["evidence_refs"] = [{"url": foreign_url, "source": "OtherWire"}]
        other = deepcopy(article_input())
        other["event_id"] = "event-other"
        other["evidence"] = [
            {
                "source": "OtherWire",
                "url": foreign_url,
                "title": "Other event",
                "summary": "Unrelated.",
            }
        ]
        result = run_article_qa(article, article_input(), other_article_inputs=[other])
        self.assertIn("foreign_evidence_ref", _codes(result, severity="critical"))

    def test_all_body_claims_grounded_passes(self) -> None:
        result = run_article_qa(structured_exploit_article(), article_input())
        self.assertTrue(result["publishable"])
        self.assertEqual(result["metrics"]["uncovered_assertive_sentence_count"], 0)

    def test_compound_unit_can_reference_multiple_claims(self) -> None:
        article = structured_regulatory_article()
        first = article["article_sections"][0]["paragraphs"][0]
        second_id = article["claims"][1]["claim_id"]
        if second_id not in first["claim_ids"]:
            first["claim_ids"] = list(first["claim_ids"]) + [second_id]
        result = run_article_qa(article, article_input())
        self.assertNotIn("unknown_claim_id", _codes(result, severity="critical"))
        self.assertGreaterEqual(len(first["claim_ids"]), 2)

    def test_quote_body_requires_quote_ledger(self) -> None:
        article = structured_regulatory_article()
        article["quotes"] = []
        article["article_sections"][0]["paragraphs"][0]["text"] = (
            article["article_sections"][0]["paragraphs"][0]["text"]
            + ' Northwind said, "12,400 customer records were involved."'
        )
        materialize_article(article)
        result = run_article_qa(article, article_input())
        self.assertIn("quote_body_unmapped", _codes(result, severity="critical"))

    def test_generic_unsupported_conclusion_fails(self) -> None:
        article = structured_regulatory_article()
        article["article_sections"].append(
            {
                "section_id": "s-close",
                "purpose": "unsupported",
                "paragraphs": [
                    {
                        "text": (
                            "This development underscores growing industry-wide risk for payments "
                            "platforms and stakeholders will be watching the outcome closely."
                        ),
                        "claim_ids": [article["claims"][0]["claim_id"]],
                    }
                ],
            }
        )
        materialize_article(article)
        result = run_article_qa(article, article_input())
        self.assertFalse(result["publishable"])
        self.assertTrue(
            {"ungrounded_contextual_assertion", "body_assertion_not_in_claims"}
            & _codes(result, severity="critical")
        )

    def test_no_conclusion_required(self) -> None:
        article = structured_regulatory_article()
        last = article["article_sections"][-1]["paragraphs"][0]["text"].lower()
        self.assertNotIn("underscores", last)
        self.assertNotIn("will be watching", last)
        result = run_article_qa(article, article_input())
        self.assertTrue(result["publishable"])

    def test_explicit_insufficient_depth_failure_accepted(self) -> None:
        normalized = normalize_batch_output(
            {
                "articles": [],
                "failures": [
                    {
                        "event_id": "event-thin",
                        "code": "insufficient_depth",
                        "reason": "evidence cannot safely support 350 factual words",
                    }
                ],
            },
            requested_ids=["event-thin"],
        )
        self.assertEqual(normalized["failures_by_id"]["event-thin"]["code"], "insufficient_depth")
        self.assertTrue(normalized["completeness"]["ok"])


class Make6ReplayTests(unittest.TestCase):
    def test_old_make6_outputs_remain_failed(self) -> None:
        expected_words = {
            "event-005": 318,
            "event-020": 271,
            "event-028": 261,
            "event-032": 214,
        }
        for event_id, words in expected_words.items():
            payload = _load_make6(event_id)
            pack = payload.get("article_input") or (payload.get("qa_result") and None)
            # Story files omit full article_input; replay the frozen article against a thin pack
            # plus evidence_used URL so schema refs resolve.
            article = payload["article"]
            pack = {
                "event_id": event_id,
                "evidence": [
                    dict(ref) for ref in article.get("evidence_used") or []
                ],
            }
            for item in pack["evidence"]:
                item["title"] = article.get("headline")
                item["summary"] = article.get("dek")
                item["extracted_text"] = " ".join(
                    str(claim.get("text") or "") for claim in article.get("claims") or []
                )
            result = run_article_qa(article, pack)
            self.assertFalse(result["publishable"], event_id)
            self.assertIn("below_article_minimum_length", _codes(result, severity="critical"))
            self.assertLess(result["metrics"]["article_word_count"], 350)
            self.assertEqual(result["metrics"]["article_word_count"], words)


class CorrectedSyntheticTests(unittest.TestCase):
    def test_corrected_regulatory_fixture_passes(self) -> None:
        article = structured_regulatory_article()
        result = run_article_qa(article, article_input())
        self.assertTrue(result["publishable"])
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 450)
        self.assertLessEqual(result["metrics"]["article_word_count"], 800)
        self.assertEqual(article["category"], "regulatory")
        self.assertNotIn("exact_phrase_overlap", _codes(result, severity="critical"))

    def test_corrected_exploit_fixture_passes(self) -> None:
        article = structured_exploit_article()
        result = run_article_qa(article, article_input())
        self.assertTrue(result["publishable"])
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 450)
        self.assertEqual(article["category"], "exploit_hack")


class BudgetAndArchitectureTests(unittest.TestCase):
    def test_thresholds_and_single_call(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(MIN_EXTRACTED_WORDS, 80)
        self.assertEqual(MIN_DISTINCT_FACTS, 8)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        self.assertEqual(WARN_SENTENCE_SIMILARITY, 0.84)
        self.assertEqual(DEFAULT_CANDIDATE_SCAN_LIMIT, 12)
        self.assertEqual(BATCH_MAX_COMPLETION_TOKENS, 16384)
        self.assertEqual(BATCH_RUNNER_SRC.count("post_chat_completion("), 1)
        self.assertNotIn("repair", BATCH_RUNNER_SRC.lower())
        self.assertIn("select_viable_stories", LIVE_SRC)
        self.assertIn('if not row.get("qa_publishable")', Path(inspect.getsourcefile(live_mod)).read_text(encoding="utf-8") if False else (REPO / "src/newsagent_v2/batch/runner.py").read_text(encoding="utf-8"))
        self.assertTrue(wordpress_disabled_publish()["wordpress_disabled"])
        schema = article_output_json_schema()
        self.assertNotIn("article_body", schema["required"])
        self.assertNotIn("evidence_used", schema["required"])
        self.assertNotIn("generation_notes", schema["required"])
        self.assertIn("article_sections", schema["required"])
        self.assertNotIn("uniqueItems", json.dumps(schema))
        self.assertEqual(schema["properties"]["article_sections"]["minItems"], 5)
        self.assertIn("evidence_ids", json.dumps(schema["properties"]["claims"]))
        folded = " ".join((BATCH_SYSTEM_PROMPT + " " + SYSTEM_PROMPT).split())
        self.assertIn("Do not emit article_body", folded)

    def test_four_and_five_story_admission_under_target(self) -> None:
        stories = _load_make5_stories()
        four = build_top5_article_batch_request_body(batch_id="make6-replay", stories=stories)
        four_diag = safe_chat_request_diagnostics(four)
        self.assertLessEqual(four_diag["estimated_admission_tokens"], GROQ_BATCH_ADMISSION_TARGET)
        five = []
        for index in range(5):
            row = deepcopy(stories[index % len(stories)])
            row["event_id"] = f"event-syn-{index:03d}"
            row["article_input"] = deepcopy(row["article_input"])
            row["article_input"]["event_id"] = row["event_id"]
            five.append(row)
        five_diag = safe_chat_request_diagnostics(
            build_top5_article_batch_request_body(batch_id="five-struct", stories=five)
        )
        self.assertLessEqual(five_diag["estimated_admission_tokens"], GROQ_BATCH_ADMISSION_TARGET)
        self.assertLess(five_diag["estimated_admission_tokens"], GROQ_ON_DEMAND_TPM_LIMIT)
        self.assertEqual(four_diag["max_completion_tokens"], 16384)
        rows = [_sufficient_row(f"event-00{i}") for i in range(1, 6)]
        poster = RecordingPost(
            [FakeResponse(200, _payload([_article(f"event-00{i}") for i in range(1, 6)]))]
        )
        generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="struct-one")
        self.assertEqual(len(poster.calls), 1)


if __name__ == "__main__":
    unittest.main()


