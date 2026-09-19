from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.writer.protocol import FrozenStoryPackage
from newsagent_v2.article.writer.qwen_vllm import (
    KEY_ENV,
    BASE_ENV,
    QWEN_MODEL,
    QwenVLLMWriterProvider,
    load_qwen_config,
    parse_chat_payload,
)
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID, SOURCE_BATCH_ID
from newsagent_v2.bench.writer_bakeoff.qwen_audition import run_audition
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class QwenVLLMWriterTests(unittest.TestCase):
    def test_missing_credentials_make_zero_calls(self) -> None:
        result = run_audition(environ={}, persist=False)
        self.assertEqual(result["qwen_api_key_present"], "NO")
        self.assertEqual(result["qwen_base_url_present"], "NO")
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["groq_calls"], 0)
        self.assertEqual(result["gemini_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertTrue(result["qa_unchanged"])
        self.assertEqual(result["qa_policy"]["hard_minimum_words"], 350)

    def test_build_request_uses_exact_model_and_frozen_compact(self) -> None:
        config = load_qwen_config(
            {KEY_ENV: "test-key-not-real-aaaaaaaa", BASE_ENV: "https://example.invalid/v1"}
        )
        provider = QwenVLLMWriterProvider(config)
        story = FrozenStoryPackage(event_id=EVENT_ID, article_input={"event_id": EVENT_ID, "evidence": []})
        compact = {
            "event_id": EVENT_ID,
            "evidence_units": [{"evidence_id": "event-005-e01", "text": "frozen only"}],
        }
        body = provider.build_request(story, compact=compact)
        self.assertEqual(body["model"], QWEN_MODEL)
        self.assertNotIn("test-key-not-real-aaaaaaaa", json.dumps(body))
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(user["evidence_units"][0]["text"], "frozen only")
        self.assertEqual(user["event_id"], EVENT_ID)

    def test_parse_strips_think_and_keeps_json(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        payload = {
            "choices": [
                {
                    "message": {
                        "reasoning_content": "secret chain of thought",
                        "content": f"<think>do not persist</think>\n{json.dumps(native)}",
                    }
                }
            ]
        }
        parsed = parse_chat_payload(payload)
        self.assertEqual(parsed["headline"], native["headline"])
        self.assertNotIn("do not persist", json.dumps(parsed))

    def test_mocked_transport_one_generation_no_groq_gemini(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        calls: list[str] = []

        def transport(url, *, method, headers, json_body, timeout):
            del url, headers, timeout
            calls.append(method)
            if method == "GET":
                return DummyResponse(200, {"data": [{"id": QWEN_MODEL}]})
            self.assertEqual(json_body["model"], QWEN_MODEL)
            return DummyResponse(
                200,
                {
                    "choices": [{"message": {"content": json.dumps(native)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                },
            )

        result = run_audition(
            environ={
                KEY_ENV: "test-key-not-real-bbbbbbbb",
                BASE_ENV: "https://example.invalid/v1",
            },
            transport=transport,
            persist=False,
        )
        self.assertEqual(calls, ["GET", "POST"])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["exact_model_used"], QWEN_MODEL)
        self.assertEqual(result["groq_calls"], 0)
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["native_parse"], "PASS")
        self.assertEqual(SOURCE_BATCH_ID, "20260915T081205Z-2b732ddc")
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_sanitize_error_redacts_hosts_and_urls(self) -> None:
        from newsagent_v2.article.writer.qwen_vllm import sanitize_error

        raw = (
            "HTTPSConnectionPool(host='exclude-spouse-numerical-linda.trycloudflare.com', port=443): "
            "Max retries exceeded with url: /v1/models (Caused by NameResolutionError("
            "\"Failed to resolve 'exclude-spouse-numerical-linda.trycloudflare.com'\"))"
        )
        cleaned = sanitize_error(raw, ("secret-key-value",))
        self.assertNotIn("trycloudflare", cleaned)
        self.assertNotIn("exclude-spouse", cleaned)
        self.assertIn("[redacted-host]", cleaned)

    def test_isolation_tokens_absent(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        for rel in (
            "src/newsagent_v2/article/writer/qwen_vllm.py",
            "src/newsagent_v2/bench/writer_bakeoff/qwen_audition.py",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            for token in blocked:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


