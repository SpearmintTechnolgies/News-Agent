from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.groq_oss20 import MODEL_NAME, groq_gpt_oss_20b_controlled_v3_request
from newsagent_v2.article.writer.schema import groq_controlled_v3_json_schema
from newsagent_v2.bench.writer_bakeoff.contract import (
    EVENT_ID,
    GROQ_GPT_OSS_20B_MODEL,
    GROQ_MODEL,
)
from newsagent_v2.bench.writer_bakeoff.controlled_v3_gpt_oss_20b_audition import run_audition
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self) -> dict:
        return self._payload


class ControlledV3GptOss20bAuditionTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_audition(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], "openai/gpt-oss-20b")
        self.assertEqual(result["gpt_oss_120b_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["qa_changed"], "NO")
        self.assertEqual(result["kimi_inference_blocked"], "YES")
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        self.assertFalse(live_make_can_select_kimi())
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertNotEqual(GROQ_GPT_OSS_20B_MODEL, GROQ_MODEL)

    def test_schema_omits_ledger_and_uses_gpt_oss_20b(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        pack = article_input_for_ledgers(fixture["article_input"])
        ledgers = build_evidence_ledgers(pack)
        plan = plan_article(ledgers, pack)
        body = groq_gpt_oss_20b_controlled_v3_request(plan, ledgers)
        schema = body["response_format"]["json_schema"]["schema"]
        props = schema.get("properties") or {}
        self.assertEqual(body["model"], "openai/gpt-oss-20b")
        self.assertEqual(MODEL_NAME, "openai/gpt-oss-20b")
        self.assertNotEqual(body["model"], "openai/gpt-oss-120b")
        self.assertIn("reasoning_effort", body)
        self.assertFalse(body["include_reasoning"])
        self.assertIn("paragraphs", props)
        self.assertNotIn("claims", props)
        self.assertNotIn("quotes", props)
        self.assertNotIn("article_body", props)
        self.assertNotIn("claims", groq_controlled_v3_json_schema().get("properties") or {})
        blob = json.dumps(body["messages"])
        self.assertIn("PRE-APPROVED", blob)
        self.assertIn("proposition_frames", blob)
        self.assertNotIn("allowed_semantic_facts", blob)
        self.assertNotIn("allowed_claims", blob)
        self.assertNotIn("Write a news article about", blob)
        self.assertNotIn("DEVELOPMENT_WINNER", blob)

    def test_mocked_one_call_hits_gpt_oss_20b_only(self) -> None:
        calls: list[dict] = []

        def http_post(url, *, headers, json, timeout):
            del timeout
            self.assertNotIn("Authorization", json)
            blob = __import__("json").dumps(json)
            self.assertNotIn("test-not-a-real-key", blob)
            self.assertNotIn("openai/gpt-oss-120b", blob)
            self.assertNotIn("qwen/qwen3.8-27b", blob)
            self.assertNotIn("gemini", blob.lower())
            self.assertEqual(json["model"], "openai/gpt-oss-20b")
            user = __import__("json").loads(json["messages"][1]["content"])
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
                "event_id": EVENT_ID,
                "headline": "Senate Republicans offer revised CLARITY Act text",
                "dek": "A procedural vote is scheduled as ethics rules are rewritten.",
                "paragraphs": paragraphs,
                "seo_title": "Senate Republicans offer revised CLARITY Act text",
                "meta_description": "Republicans released revised CLARITY Act text before a key procedural vote.",
                "slug": "senate-republicans-revised-clarity-act",
                "entities": [{"name": "Cynthia Lummis", "type": "person"}],
                "keywords": ["CLARITY Act"],
            }
            calls.append({"url": url, "headers": headers})
            return DummyResponse(
                {
                    "choices": [{"message": {"content": __import__("json").dumps(native)}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
                }
            )

        result = run_audition(
            environ={GROQ_KEY_ENV: "test-not-a-real-key"},
            http_post=http_post,
            persist=False,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], GROQ_CHAT_COMPLETIONS_URL)
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["gpt_oss_20b_calls"], 1)
        self.assertEqual(result["gpt_oss_120b_calls"], 0)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["kimi_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertTrue(result["evidence_claim_ledger_unchanged"])
        self.assertTrue(result["quote_ledger_unchanged"])
        self.assertEqual(result["secret_exposure_check"], "PASS")
        self.assertNotIn("test-not-a-real-key", json.dumps(result, default=str))


if __name__ == "__main__":
    unittest.main()


