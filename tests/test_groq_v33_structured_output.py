from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY
from newsagent_v2.article.writer.controlled.groq_oss20 import (
    GroqGptOss20bProseRenderer,
    groq_gpt_oss_20b_controlled_v3_request,
    parse_controlled_v3_native,
)
from newsagent_v2.article.writer.controlled.groq_structured import (
    GroqStructuredOutputError,
    assert_groq_root_object_schema,
    decode_strict_root_object,
    sanitized_request_snapshot,
)
from newsagent_v2.article.writer.controlled.paragraph import OUTCOME_PARTIALLY_RETAINED
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.controlled.renderer import CONTROLLED_WRITER_V3_SYSTEM_PROMPT
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.schema import groq_controlled_v3_json_schema
from newsagent_v2.benchmark.input import write_json_utf8
from tests.test_controlled_v33_gpt_oss_20b_proof import DummyResponse
from tests.test_controlled_writer_v3 import _rich_story

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "benchmarks" / "writer_bakeoff" / "controlled_v33_fresh" / "NEXT_LIVE_REQUEST_SNAPSHOT.json"


def _native_for_plan(plan, *, extra: dict | None = None) -> dict:
    paragraphs = []
    for para in plan.paragraph_plans:
        sentences = [
            {
                "sentence_id": f"{para.paragraph_id}-s1",
                "text": "Northwind Payments disclosed the records after the spoofed email review.",
                "fact_ids_used": [para.allowed_claim_ids[0]],
                "quote_ids_used": [],
            }
        ]
        paragraphs.append({"paragraph_id": para.paragraph_id, "sentences": sentences})
    native = {
        "event_id": plan.event_id,
        "headline": "Northwind Payments disclosed spoofed-email customer-file review",
        "dek": "The firm said it is reviewing affected retail customer records.",
        "paragraphs": paragraphs,
        "seo_title": "Northwind Payments disclosed spoofed-email customer-file review",
        "meta_description": "Northwind Payments disclosed a review of spoofed-email customer records.",
        "slug": "northwind-payments-disclosed-review",
        "entities": [{"name": "Northwind Payments", "type": "org"}],
        "keywords": ["Northwind"],
    }
    if extra:
        native.update(extra)
    return native


class GroqV33StructuredOutputTests(unittest.TestCase):
    def test_root_schema_object_forbids_root_array(self) -> None:
        schema = groq_controlled_v3_json_schema()
        preflight = assert_groq_root_object_schema(schema)
        self.assertEqual(preflight["root_schema_type"], "object")
        self.assertFalse(preflight["root_array_allowed"])
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["paragraphs"]["type"], "array")
        sentences = schema["properties"]["paragraphs"]["items"]["properties"]["sentences"]
        self.assertEqual(sentences["type"], "array")
        item = sentences["items"]["properties"]
        self.assertEqual(item["fact_ids_used"]["type"], "array")
        self.assertEqual(item["quote_ids_used"]["type"], "array")
        self.assertNotIn("text", schema["properties"]["paragraphs"]["items"]["properties"])
        with self.assertRaises(GroqStructuredOutputError):
            assert_groq_root_object_schema({"type": "array", "items": {"type": "object"}})

    def test_decode_accepts_one_object_rejects_wrappers(self) -> None:
        story = _rich_story("event-fresh-ok")
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        native = _native_for_plan(plan)
        self.assertEqual(decode_strict_root_object(native)["headline"], native["headline"])
        self.assertEqual(decode_strict_root_object(json.dumps(native))["event_id"], plan.event_id)
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object([native])
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object([])
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object(json.dumps([native]))
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object(json.dumps(native) + json.dumps(native))
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object("```json\n" + json.dumps(native) + "\n```")
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object({**native, "unexpected": "nope"})
        missing = dict(native)
        missing.pop("headline")
        with self.assertRaises(GroqStructuredOutputError):
            decode_strict_root_object(missing)
        parsed = parse_controlled_v3_native(native, plan)
        self.assertTrue(parsed.ok)
        wrapped = parse_controlled_v3_native([native], plan)  # type: ignore[arg-type]
        self.assertFalse(wrapped.ok)
        self.assertTrue(wrapped.invalid_output)

    def test_mocked_root_object_reaches_renderer_validation(self) -> None:
        story = _rich_story("event-fresh-ok")
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        native = _native_for_plan(plan)
        calls: list[int] = []

        def http_post(url, *, headers, json, timeout):
            del url, headers, timeout
            self.assertEqual(json["model"], "openai/gpt-oss-20b")
            self.assertEqual(json["response_format"]["json_schema"]["schema"]["type"], "object")
            self.assertIn("EXACTLY ONE JSON OBJECT", json["messages"][0]["content"])
            calls.append(1)
            return DummyResponse(
                {
                    "choices": [{"message": {"content": __import__("json").dumps(native)}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }
            )

        renderer = GroqGptOss20bProseRenderer(api_key="test-not-a-real-key", http_post=http_post)
        result = renderer.render(plan, ledgers)
        self.assertEqual(calls, [1])
        self.assertTrue(result.ok)
        self.assertFalse(result.provider_error)
        self.assertTrue(result.paragraphs)
        self.assertEqual(renderer.schema_preflight["root_schema_type"], "object")

        def http_array(url, *, headers, json, timeout):
            del url, headers, json, timeout
            return DummyResponse({"choices": [{"message": {"content": __import__("json").dumps([native])}}]})

        bad = GroqGptOss20bProseRenderer(api_key="test-not-a-real-key", http_post=http_array)
        rejected = bad.render(plan, ledgers)
        self.assertFalse(rejected.ok)
        self.assertTrue(rejected.invalid_output or rejected.provider_error)
        self.assertIsNone(bad.native)

        def http_empty_array(url, *, headers, json, timeout):
            del url, headers, json, timeout
            return DummyResponse({"choices": [{"message": {"content": "[]"}}]})

        empty = GroqGptOss20bProseRenderer(api_key="test-not-a-real-key", http_post=http_empty_array)
        self.assertFalse(empty.render(plan, ledgers).ok)

        def http_malformed(url, *, headers, json, timeout):
            del url, headers, json, timeout
            return DummyResponse({"choices": [{"message": {"content": "{not json"}}]})

        malformed = GroqGptOss20bProseRenderer(api_key="test-not-a-real-key", http_post=http_malformed)
        self.assertFalse(malformed.render(plan, ledgers).ok)

    def test_failed_generation_is_never_salvaged(self) -> None:
        story = _rich_story("event-fresh-ok")
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        salvage = json.dumps([_native_for_plan(plan)])

        def http_post(url, *, headers, json, timeout):
            del url, headers, json, timeout
            return DummyResponse(
                {
                    "error": {
                        "message": "expected object, but got array",
                        "code": "json_validate_failed",
                        "failed_generation": salvage,
                    }
                },
                status_code=400,
            )

        renderer = GroqGptOss20bProseRenderer(api_key="test-not-a-real-key", http_post=http_post)
        result = renderer.render(plan, ledgers)
        self.assertFalse(result.ok)
        self.assertTrue(result.provider_error)
        self.assertIsNone(renderer.native)
        self.assertIsNone(result.native)
        self.assertNotIn("Northwind", json.dumps(result.paragraphs))

    def test_invariants_and_snapshot(self) -> None:
        self.assertIn("ONE SENTENCE = ONE PRIMARY FACTUAL ASSERTION", CONTROLLED_WRITER_V3_SYSTEM_PROMPT)
        self.assertEqual(OUTCOME_PARTIALLY_RETAINED, "PARTIALLY_RETAINED")
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        story = _rich_story("event-fresh-ok")
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        body = groq_gpt_oss_20b_controlled_v3_request(plan, ledgers)
        snap = sanitized_request_snapshot(body)
        self.assertEqual(snap["model"], "openai/gpt-oss-20b")
        self.assertEqual(snap["root_schema_type"], "object")
        self.assertTrue(snap["prompt_requires_one_root_object"])
        self.assertFalse(snap["root_array_example_present"])
        self.assertFalse(snap["secrets_present"])
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        write_json_utf8(SNAPSHOT, snap)
        self.assertTrue(SNAPSHOT.is_file())
        self.assertNotIn("GROQ_API_KEY", SNAPSHOT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


