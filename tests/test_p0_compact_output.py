from __future__ import annotations

import inspect
import json
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.batch_prompt import BATCH_SYSTEM_PROMPT, batch_output_json_schema
from newsagent_v2.article.batch_runner import generate_make_articles, normalize_batch_output
from newsagent_v2.article.enrich import MIN_DISTINCT_FACTS, MIN_EXTRACTED_WORDS
from newsagent_v2.article.expand import evidence_id_index, expand_provider_article, resolve_evidence_ids
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
from newsagent_v2.providers.groq_editorial import estimate_prompt_tokens_from_bytes, safe_chat_request_diagnostics
from tests.fixtures.article_qa import (
    EVIDENCE_URL,
    article_input,
    clean_article,
    structured_regulatory_article,
)
from tests.test_article_qa import _codes
from tests.test_groq_editorial_benchmark import FakeResponse
from tests.test_top5_single_call import ENV, RecordingPost, _article, _payload, _sufficient_row

REPO = Path(__file__).resolve().parents[1]
MAKE7 = REPO / "output" / "approval" / "20260915T071836Z-29db60a4" / "batch.json"
BATCH_RUNNER_SRC = (REPO / "src" / "newsagent_v2" / "article" / "batch_runner.py").read_text(
    encoding="utf-8"
)
LIVE_SRC = inspect.getsource(live_mod)
OUTPUT_CAP = 16384
SAFE_OUTPUT_RATIO = 0.90

_SENTENCE = (
    "Northwind Payments told TestWire that 12,400 customer records were exposed "
    "after a spoofed government-domain email reached staff during the Monday recap."
)


def _load_make7_stories() -> list[dict]:
    payload = json.loads(MAKE7.read_text(encoding="utf-8"))
    viability = payload["telemetry"]["viability"]
    selected = list(viability["final_selected_ids"])
    by_id = {
        row["event_id"]: row
        for row in viability["stories"]
        if isinstance(row, dict) and isinstance(row.get("article_input"), dict)
    }
    return [by_id[event_id] for event_id in selected if event_id in by_id]


def _primary_evidence_id(pack: dict | None = None) -> str:
    index = evidence_id_index(pack or article_input())
    for key in index:
        if str(key).startswith("event-"):
            return str(key)
    return next(iter(index))


def _to_compact(article: dict, evidence_id: str) -> dict:
    article = deepcopy(article)
    for claim in article.get("claims") or []:
        claim["id"] = str(claim.get("claim_id") or claim.get("id"))
        claim["evidence_ids"] = [evidence_id]
        claim.pop("claim_id", None)
        claim.pop("evidence_refs", None)
    for quote in article.get("quotes") or []:
        quote["evidence_ids"] = [evidence_id]
        quote.pop("evidence_refs", None)
    for section in article.get("article_sections") or []:
        section["id"] = str(section.get("section_id") or section.get("id") or "s1")
        section.pop("section_id", None)
        section.pop("purpose", None)
    article.pop("article_body", None)
    article.pop("evidence_used", None)
    article.pop("generation_notes", None)
    article.pop("schema_version", None)
    return article


def _pad_words(target: int, prefix: str = "") -> str:
    words = prefix.split()
    filler = _SENTENCE.split()
    index = 0
    while len(words) < target:
        words.append(filler[index % len(filler)])
        index += 1
    return " ".join(words[:target])


def _compact_size_article(
    event_id: str,
    *,
    target_words: int,
    evidence_id: str,
    url: str,
) -> dict:
    claim_count = 12
    section_count = 6
    per_section = max(60, target_words // section_count)
    claims = []
    sections = []
    for index in range(1, claim_count + 1):
        claims.append(
            {
                "id": f"c{index}",
                "text": f"{event_id} claim {index}: Northwind reported 12,400 records after the spoofed mailbox.",
                "claim_type": "fact",
                "evidence_ids": [evidence_id],
            }
        )
    remaining = target_words
    for index in range(1, section_count + 1):
        words_here = remaining if index == section_count else per_section
        remaining -= words_here
        start = (index - 1) * 2
        claim_ids = [f"c{start + 1}", f"c{start + 2}"]
        sections.append(
            {
                "id": f"s{index}",
                "paragraphs": [{"text": _pad_words(words_here, event_id), "claim_ids": claim_ids}],
            }
        )
    return {
        "event_id": event_id,
        "headline": f"{event_id} Northwind reports 12,400-record exposure",
        "dek": "A spoofed government email was used to obtain customer files.",
        "category": "security_incident",
        "seo_title": f"{event_id} Northwind customer records exposed",
        "meta_description": "Northwind Payments said 12,400 customer records were exposed after a spoofed email.",
        "slug": f"{event_id}-northwind-records-exposed",
        "entities": [{"name": "Northwind Payments", "type": "org"}],
        "keywords": ["northwind", "data exposure"],
        "claims": claims,
        "quotes": [
            {
                "text": "12,400 customer records were involved",
                "kind": "direct",
                "attribution": "Northwind Payments",
                "evidence_ids": [evidence_id],
            }
        ],
        "article_sections": sections,
        "_debug_url": url,
    }


def _verbose_twin(article: dict, url: str, source: str = "TestWire") -> dict:
    row = deepcopy(article)
    ref = {"url": url, "source": source}
    for claim in row["claims"]:
        claim["claim_id"] = claim.pop("id")
        claim["evidence_refs"] = [ref]
        claim.pop("evidence_ids", None)
    for quote in row["quotes"]:
        quote["evidence_refs"] = [ref]
        quote.pop("evidence_ids", None)
    for section in row["article_sections"]:
        section["section_id"] = section.pop("id")
        section["purpose"] = "facts"
    row["evidence_used"] = [ref]
    row["generation_notes"] = "Article meets minimum length requirement and all claims are supported."
    row["article_body"] = "\n\n".join(
        para["text"] for section in row["article_sections"] for para in section["paragraphs"]
    )
    return row


def _batch_json(articles: list[dict], *, failures: list | None = None) -> str:
    payload = {"failures": failures if failures is not None else [], "articles": articles}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class CompactEvidenceIdTests(unittest.TestCase):
    def test_compact_evidence_id_resolves_correctly(self) -> None:
        pack = article_input()
        evidence_id = _primary_evidence_id(pack)
        compact = _to_compact(structured_regulatory_article(), evidence_id)
        expand_provider_article(compact, pack)
        refs = compact["claims"][0]["evidence_refs"]
        self.assertEqual(refs[0]["url"], EVIDENCE_URL)
        self.assertEqual(refs[0]["source"], "TestWire")
        self.assertEqual(compact["evidence_used"][0]["url"], EVIDENCE_URL)
        materialize_article(compact)
        result = run_article_qa(compact, pack)
        self.assertTrue(result["publishable"])

    def test_unknown_evidence_id_fails(self) -> None:
        article = _to_compact(structured_regulatory_article(), "missing-e99")
        result = run_article_qa(article, article_input())
        self.assertIn("unknown_evidence_ref", _codes(result, severity="critical"))

    def test_foreign_event_evidence_id_fails(self) -> None:
        other = deepcopy(article_input())
        other["event_id"] = "event-other"
        other["evidence"] = deepcopy(other["evidence"])
        other["evidence"][0]["url"] = "https://example.com/other-event"
        other["evidence"][0]["extracted_text"] = other["evidence"][0]["summary"]
        foreign_id = _primary_evidence_id(other)
        self.assertTrue(str(foreign_id).startswith("event-other"))
        article = _to_compact(structured_regulatory_article(), foreign_id)
        result = run_article_qa(article, article_input(), other_article_inputs=[other])
        self.assertIn("foreign_evidence_ref", _codes(result, severity="critical"))

    def test_claim_without_evidence_ids_fails(self) -> None:
        article = structured_regulatory_article()
        article["claims"][0]["evidence_ids"] = []
        article["claims"][0]["evidence_refs"] = []
        result = run_article_qa(article, article_input())
        self.assertIn("claim_missing_evidence", _codes(result, severity="critical"))

    def test_orphan_claim_fails(self) -> None:
        article = structured_regulatory_article()
        article["claims"].append(
            {
                "claim_id": "orphan-c",
                "text": "An extra unused Northwind assertion.",
                "claim_type": "fact",
                "evidence_refs": [{"url": EVIDENCE_URL, "source": "TestWire"}],
            }
        )
        result = run_article_qa(article, article_input())
        self.assertIn("orphan_claim", _codes(result, severity="critical"))

    def test_resolve_helper_skips_unknown(self) -> None:
        refs = resolve_evidence_ids(["nope", _primary_evidence_id()], article_input())
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["url"], EVIDENCE_URL)


class FailuresFirstTests(unittest.TestCase):
    def test_schema_emits_failures_before_articles(self) -> None:
        schema = batch_output_json_schema()
        self.assertEqual(schema["required"], ["failures", "articles"])
        self.assertEqual(list(schema["properties"].keys())[0], "failures")

    def test_failures_first_parsing_works(self) -> None:
        article = clean_article()
        parsed = {"failures": [], "articles": [article]}
        normalized = normalize_batch_output(parsed, requested_ids=[article["event_id"]])
        self.assertIn(article["event_id"], normalized["articles_by_id"])
        self.assertEqual(normalized["failures_by_id"], {})

    def test_empty_failures_accepted(self) -> None:
        article = clean_article()
        parsed = {"articles": [article], "failures": []}
        normalized = normalize_batch_output(parsed, requested_ids=[article["event_id"]])
        self.assertTrue(normalized["completeness"]["ok"])

    def test_explicit_story_failure_accepted(self) -> None:
        normalized = normalize_batch_output(
            {
                "failures": [
                    {
                        "event_id": "event-thin",
                        "code": "insufficient_depth",
                        "reason": "evidence cannot safely support 350 factual words",
                    }
                ],
                "articles": [],
            },
            requested_ids=["event-thin"],
        )
        self.assertEqual(normalized["failures_by_id"]["event-thin"]["code"], "insufficient_depth")
        self.assertTrue(normalized["completeness"]["ok"])


class OutputBudgetTests(unittest.TestCase):
    def test_completion_cap_is_16384(self) -> None:
        self.assertEqual(BATCH_MAX_COMPLETION_TOKENS, OUTPUT_CAP)
        self.assertEqual(BATCH_MAX_COMPLETION_TOKENS, 16384)

    def test_make7_reconstructed_admission_under_target(self) -> None:
        stories = _load_make7_stories()
        self.assertEqual(len(stories), 4)
        body = build_top5_article_batch_request_body(batch_id="make7-replay", stories=stories)
        diag = safe_chat_request_diagnostics(body)
        self.assertLessEqual(diag["estimated_admission_tokens"], GROQ_BATCH_ADMISSION_TARGET)
        self.assertLess(diag["estimated_admission_tokens"], GROQ_ON_DEMAND_TPM_LIMIT)
        self.assertEqual(diag["max_completion_tokens"], 16384)
        self.assertTrue(diag["admission_excludes_max_completion_tokens"])
        schema = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["required"][0], "failures")
        self.assertNotIn("uniqueItems", json.dumps(schema))
        self.assertNotIn("evidence_refs", json.dumps(article_output_json_schema()))

    def _measure(self, articles: list[dict]) -> dict[str, int]:
        compact = [ {k: v for k, v in row.items() if not str(k).startswith("_")} for row in articles ]
        blob = _batch_json(compact)
        encoded = blob.encode("utf-8")
        return {
            "characters": len(blob),
            "bytes": len(encoded),
            "tokens": estimate_prompt_tokens_from_bytes(len(encoded)),
        }

    def test_four_and_five_story_output_fits(self) -> None:
        url = "https://cointelegraph.com/news/us-republicans-send-final-clarity-act-offer-to-democrats"
        four_500 = [
            _compact_size_article(f"event-00{i}", target_words=500, evidence_id=f"event-00{i}-e01", url=url)
            for i in range(1, 5)
        ]
        five_500 = [
            _compact_size_article(f"event-00{i}", target_words=500, evidence_id=f"event-00{i}-e01", url=url)
            for i in range(1, 6)
        ]
        five_650 = [
            _compact_size_article(f"event-00{i}", target_words=650, evidence_id=f"event-00{i}-e01", url=url)
            for i in range(1, 6)
        ]
        m4 = self._measure(four_500)
        m5 = self._measure(five_500)
        m650 = self._measure(five_650)
        verbose650 = self._measure([_verbose_twin(row, url) for row in five_650])
        self.assertLess(m4["tokens"], OUTPUT_CAP)
        self.assertLess(m5["tokens"], OUTPUT_CAP)
        self.assertLess(m650["tokens"], OUTPUT_CAP)
        self.assertLess(m650["tokens"] / OUTPUT_CAP, SAFE_OUTPUT_RATIO)
        self.assertLess(m650["bytes"], verbose650["bytes"])
        self.assertGreaterEqual(word_count(four_500[0]["article_sections"][0]["paragraphs"][0]["text"]), 60)
        # Persist measurements on the test object for the report by asserting exact bands.
        self.assertGreater(m4["tokens"], 1000)
        self.assertGreater(m5["tokens"], m4["tokens"])
        self.assertGreater(m650["tokens"], m5["tokens"])
        self._sizes = {"four_500": m4, "five_500": m5, "five_650": m650, "verbose_650": verbose650}

    def test_thresholds_and_architecture(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(MIN_EXTRACTED_WORDS, 80)
        self.assertEqual(MIN_DISTINCT_FACTS, 8)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        self.assertEqual(WARN_SENTENCE_SIMILARITY, 0.84)
        self.assertEqual(DEFAULT_CANDIDATE_SCAN_LIMIT, 12)
        self.assertEqual(BATCH_RUNNER_SRC.count("post_chat_completion("), 1)
        self.assertNotIn("repair", BATCH_RUNNER_SRC.lower())
        self.assertIn("select_viable_stories", LIVE_SRC)
        self.assertTrue(wordpress_disabled_publish()["wordpress_disabled"])
        self.assertIn("qa_publishable", (REPO / "src/newsagent_v2/batch/runner.py").read_text(encoding="utf-8"))
        folded = " ".join((BATCH_SYSTEM_PROMPT + " " + SYSTEM_PROMPT).split())
        self.assertIn("Emit the top-level `failures` array FIRST", folded)
        self.assertIn("Do not emit evidence_used or generation_notes", folded)
        rows = [_sufficient_row(f"event-00{i}") for i in range(1, 6)]
        poster = RecordingPost(
            [FakeResponse(200, _payload([_article(f"event-00{i}") for i in range(1, 6)]))]
        )
        generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="compact-one")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(poster.calls[0]["json"]["max_completion_tokens"], 16384)

    def test_renderer_still_joins_sections(self) -> None:
        article = structured_regulatory_article()
        self.assertEqual(article["article_body"], render_article_body(article))
        self.assertGreaterEqual(word_count(article["article_body"]), 350)


if __name__ == "__main__":
    unittest.main()


