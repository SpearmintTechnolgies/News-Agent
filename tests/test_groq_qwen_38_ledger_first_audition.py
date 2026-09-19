from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.article.writer.schema import groq_ledger_first_json_schema
from newsagent_v2.bench.writer_bakeoff.contract import (
    EVENT_ID,
    GROQ_MODEL,
    GROQ_QWEN_38_MODEL,
    QWEN_MODEL,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.groq_qwen_38_ledger_first_audition import run_audition
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.bench.writer_bakeoff.providers import (
    groq_qwen_38_article_first_request,
    groq_qwen_38_ledger_first_request,
    next_writer_candidate,
)
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self) -> dict:
        return self._payload


class GroqQwen38LedgerFirstAuditionTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_audition(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], GROQ_QWEN_38_MODEL)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["gpt_oss_calls"], 0)
        self.assertEqual(result["qwen_vllm_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertEqual(result["qa_unchanged"], "YES")
        self.assertEqual(result["kimi_inference_blocked"], "YES")
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        self.assertFalse(live_make_can_select_kimi())
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_request_omits_claims_and_gpt_oss_reasoning(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        body = groq_qwen_38_ledger_first_request(fixture)
        old = groq_qwen_38_article_first_request(fixture)
        schema = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(body["model"], "qwen/qwen3.8-27b")
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("include_reasoning", body)
        self.assertNotIn("claims", schema.get("properties") or {})
        self.assertNotIn("quotes", schema.get("properties") or {})
        self.assertNotIn("paragraph_maps", schema.get("properties") or {})
        self.assertIn("claims", (old["response_format"]["json_schema"]["schema"].get("properties") or {}))
        self.assertIn("closed factual ledger", json.dumps(body["messages"]))
        self.assertEqual(GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertEqual(QWEN_MODEL, "vllm-local/qwen3.8-27b")
        self.assertEqual(next_writer_candidate()["model"], "gemini-3.6-flash")
        self.assertNotIn("claims", groq_ledger_first_json_schema().get("properties") or {})

    def test_mocked_one_call_hits_groq_qwen_ledger_first_only(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        native.pop("claims", None)
        native.pop("quotes", None)
        native.pop("paragraph_maps", None)
        calls: list[dict] = []

        def http_post(url, *, headers, json, timeout):
            del headers, timeout
            calls.append({"url": url, "body": json})
            blob = __import__("json").dumps(json)
            self.assertNotIn("DEVELOPMENT_WINNER", blob)
            self.assertNotIn("openai/gpt-oss-120b", blob)
            self.assertNotIn("gemini-3.6-flash", blob)
            self.assertEqual(json["model"], "qwen/qwen3.8-27b")
            self.assertNotIn("reasoning_effort", json)
            props = ((json.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
            self.assertNotIn("claims", (props.get("properties") or {}))
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
        self.assertEqual(result["groq_calls"], 1)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["native_parse"], "PASS")
        self.assertEqual(result["normalization"], "PASS")
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["qwen_vllm_calls"], 0)
        self.assertEqual(result["gpt_oss_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertTrue(result["evidence_claim_ledger_unchanged"])
        self.assertTrue(result["quote_ledger_unchanged"])
        self.assertEqual(SOURCE_BATCH_ID, "20260915T081205Z-2b732ddc")

    def test_transient_http_does_not_retry(self) -> None:
        calls: list[int] = []

        def http_post(url, *, headers, json, timeout):
            del url, headers, json, timeout
            calls.append(1)
            return DummyResponse({"error": {"message": "high demand"}}, status_code=503)

        result = run_audition(
            environ={GROQ_KEY_ENV: "test-not-a-real-key"},
            http_post=http_post,
            persist=False,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["failure_classifications"], ["PROVIDER/API"])
        self.assertFalse(result["ok"])

    def test_isolation_tokens_absent(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        text = (
            REPO / "src/newsagent_v2/bench/writer_bakeoff/groq_qwen_38_ledger_first_audition.py"
        ).read_text(encoding="utf-8")
        for token in blocked:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


