from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.capacity import CLASS_BORDERLINE, CLASS_SUFFICIENT, article_input_for_ledgers
from newsagent_v2.article.writer.controlled.config import CAPACITY_SAFETY_MARGIN_WORDS
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID, GROQ_GPT_OSS_20B_MODEL, GROQ_MODEL
from newsagent_v2.bench.writer_bakeoff.controlled_v32_gpt_oss_20b_proof import (
    evaluate_story_capacity,
    run_proof,
    select_sufficient_story,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL
from tests.test_controlled_writer_v3 import _rich_story, _thin_story

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self) -> dict:
        return self._payload


class ControlledV32GptOss20bProofTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_proof(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], "openai/gpt-oss-20b")
        self.assertEqual(result["stage"], "credentials")
        self.assertEqual(result["gpt_oss_120b_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["qa_changed"], "NO")
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        self.assertFalse(live_make_can_select_kimi())
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertNotEqual(GROQ_GPT_OSS_20B_MODEL, GROQ_MODEL)

    def test_event005_is_borderline_and_not_selected(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        frozen = {
            "event_id": EVENT_ID,
            "original_rank": 1,
            "article_input": article_input_for_ledgers(fixture["article_input"]),
            "representative_title": "frozen",
            "sources": ["Cointelegraph"],
            "source_count": 1,
        }
        assessed = evaluate_story_capacity(frozen)
        self.assertEqual(assessed["capacity"].capacity_class, CLASS_BORDERLINE)
        self.assertFalse(assessed["safe"])
        selected, _discovery, inspections = select_sufficient_story(
            ranked_clusters=None,
            stories=[frozen, _rich_story("event-fresh-ok")],
            enrich_fn=lambda story, **_kwargs: story,
            scan_limit=12,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["story"]["event_id"], "event-fresh-ok")
        self.assertEqual(inspections[0]["status"], "skipped_frozen_event_005")
        self.assertEqual(selected["assessed"]["capacity"].capacity_class, CLASS_SUFFICIENT)

    def test_no_capacity_safe_candidate_does_not_call_groq(self) -> None:
        calls: list[dict] = []

        def http_post(*_args, **_kwargs):
            calls.append({})
            raise AssertionError("Groq must not be called")

        result = run_proof(
            environ={GROQ_KEY_ENV: "test-not-a-real-key"},
            http_post=http_post,
            persist=False,
            stories=[_thin_story()],
        )
        self.assertEqual(calls, [])
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["failure_class"], "NO_CAPACITY_SAFE_CANDIDATE")
        self.assertEqual(result["stage"], "NO_CAPACITY_SAFE_CANDIDATE")

    def test_mocked_one_call_skips_borderline_and_uses_semantic_facts(self) -> None:
        calls: list[dict] = []
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        borderline = {
            "event_id": "event-borderline-not-005",
            "original_rank": 1,
            "article_input": article_input_for_ledgers(fixture["article_input"]),
            "representative_title": "borderline",
            "sources": ["Cointelegraph"],
            "source_count": 1,
        }
        rich = _rich_story("event-fresh-ok")
        rich["original_rank"] = 2

        def http_post(url, *, headers, json, timeout):
            del timeout, headers
            self.assertEqual(json["model"], "openai/gpt-oss-20b")
            user = __import__("json").loads(json["messages"][1]["content"])
            self.assertIn("proposition_frames", __import__("json").dumps(user))
            self.assertNotIn("allowed_semantic_facts", __import__("json").dumps(user))
            self.assertNotIn("allowed_claims", __import__("json").dumps(user.get("paragraph_plans")))
            paragraphs = []
            for para in user["paragraph_plans"]:
                facts = para.get("proposition_frames") or []
                text = " ".join(
                    " ".join(
                        [
                            str(row.get("subject") or ""),
                            str(row.get("predicate") or ""),
                            str(row.get("complement") or ""),
                        ]
                    )
                    for row in facts
                )
                paragraphs.append({"paragraph_id": para["paragraph_id"], "text": text or "Northwind disclosed the records."})
            native = {
                "event_id": "event-fresh-ok",
                "headline": "Northwind Payments disclosed spoofed-email customer-file review",
                "dek": "The firm said it is reviewing affected retail customer records.",
                "paragraphs": paragraphs,
                "seo_title": "Northwind Payments disclosed spoofed-email customer-file review",
                "meta_description": "Northwind Payments disclosed a review of spoofed-email customer records.",
                "slug": "northwind-payments-disclosed-review",
                "entities": [{"name": "Northwind Payments", "type": "org"}],
                "keywords": ["Northwind"],
            }
            calls.append({"url": url})
            return DummyResponse(
                {
                    "choices": [{"message": {"content": __import__("json").dumps(native)}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
                }
            )

        result = run_proof(
            environ={GROQ_KEY_ENV: "test-not-a-real-key"},
            http_post=http_post,
            persist=False,
            stories=[borderline, rich],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], GROQ_CHAT_COMPLETIONS_URL)
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["event_id"], "event-fresh-ok")
        self.assertNotEqual(result["event_id"], EVENT_ID)
        self.assertEqual(result["gpt_oss_120b_calls"], 0)
        self.assertEqual(result["source_prose_passed_as_preferred_renderer_text"], "NO")
        self.assertEqual(result["v31_semantic_fact_active"], "YES")
        self.assertEqual(result["v32_assertion_quarantine_active"], "YES")
        self.assertEqual(result["qa_changed"], "NO")
        self.assertNotIn("test-not-a-real-key", json.dumps(result, default=str))
        self.assertGreaterEqual(CAPACITY_SAFETY_MARGIN_WORDS, 80)


if __name__ == "__main__":
    unittest.main()


