from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.config import CAPACITY_SAFETY_MARGIN_WORDS
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID, GROQ_GPT_OSS_20B_MODEL, GROQ_MODEL
from newsagent_v2.bench.writer_bakeoff.controlled_v33_gpt_oss_20b_proof import run_proof
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV
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


class ControlledV33GptOss20bProofTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_proof(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], "openai/gpt-oss-20b")
        self.assertEqual(result["stage"], "credentials")
        self.assertEqual(result["gpt_oss_120b_calls"], 0)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertNotEqual(GROQ_GPT_OSS_20B_MODEL, GROQ_MODEL)
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        self.assertFalse(live_make_can_select_kimi())

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
        self.assertEqual(result["failure_class"], "NO_CAPACITY_SAFE_CANDIDATE")

    def test_mocked_one_call_uses_v33_sentence_contract(self) -> None:
        calls: list[dict] = []
        rich = _rich_story("event-fresh-ok")
        rich["original_rank"] = 1

        def http_post(url, *, headers, json, timeout):
            del timeout, headers
            self.assertEqual(json["model"], "openai/gpt-oss-20b")
            self.assertIn("EXACTLY ONE JSON OBJECT", json["messages"][0]["content"])
            self.assertIn("fact_ids_used", json["messages"][0]["content"] + json["messages"][1]["content"])
            self.assertIn("exactly one JSON object", json["messages"][1]["content"])
            user = __import__("json").loads(json["messages"][1]["content"])
            self.assertFalse(user["requirements"]["pad_to_word_target"])
            self.assertTrue(user["requirements"]["one_sentence_one_assertion"])
            paragraphs = []
            for para in user["paragraph_plans"]:
                facts = para.get("proposition_frames") or []
                sentences = []
                for index, row in enumerate(facts, start=1):
                    text = " ".join(
                        [
                            str(row.get("subject") or ""),
                            str(row.get("predicate") or ""),
                            str(row.get("complement") or ""),
                        ]
                    ).strip() or "Northwind disclosed the records."
                    sentences.append(
                        {
                            "sentence_id": f"s{index}",
                            "text": text if text.endswith(".") else text + ".",
                            "fact_ids_used": [row.get("fact_id")],
                            "quote_ids_used": [],
                        }
                    )
                paragraphs.append({"paragraph_id": para["paragraph_id"], "sentences": sentences})
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
            stories=[rich],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], GROQ_CHAT_COMPLETIONS_URL)
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["event_id"], "event-fresh-ok")
        self.assertNotEqual(result["event_id"], EVENT_ID)
        self.assertEqual(result["v33_contract"]["raw_claim_text_exposed_to_renderer"], "NO")
        self.assertEqual(result["v33_contract"]["fact_ids_used_present"], "YES")
        self.assertFalse(result["v33_contract"]["pad_to_word_target"])
        self.assertEqual(result["v33_atomic_contract_active"], "YES")
        self.assertEqual(result["qa_changed"], "NO")
        self.assertNotIn("test-not-a-real-key", json.dumps(result, default=str))
        self.assertGreaterEqual(CAPACITY_SAFETY_MARGIN_WORDS, 80)


if __name__ == "__main__":
    unittest.main()


