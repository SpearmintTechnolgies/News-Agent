from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.bench.writer_bakeoff.contract import (
    EVENT_ID,
    GROQ_LLAMA_33_MODEL,
    GROQ_MODEL,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.llama_33_audition import run_audition
from newsagent_v2.bench.writer_bakeoff.providers import groq_article_first_request, groq_llama_33_article_first_request, next_writer_candidate
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL, GROQ_MODEL_ID

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self) -> dict:
        return self._payload


class Llama33AuditionTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_audition(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], GROQ_LLAMA_33_MODEL)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertTrue(result["qa_unchanged"])
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_live_groq_default_remains_gpt_oss(self) -> None:
        self.assertEqual(GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertEqual(GROQ_MODEL_ID, "openai/gpt-oss-120b")
        self.assertEqual(GROQ_LLAMA_33_MODEL, "llama-3.3-70b-versatile")
        self.assertEqual(next_writer_candidate()["model"], "gemini-3.6-flash")

    def test_llama_request_omits_reasoning_controls(self) -> None:
        from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture

        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        llama = groq_llama_33_article_first_request(fixture)
        groq_default = groq_article_first_request(fixture)
        self.assertEqual(llama["model"], "llama-3.3-70b-versatile")
        self.assertNotIn("reasoning_effort", llama)
        self.assertNotIn("include_reasoning", llama)
        self.assertEqual(groq_default["model"], "openai/gpt-oss-120b")
        self.assertIn("reasoning_effort", groq_default)
        blob = json.dumps(llama)
        self.assertNotIn("DEVELOPMENT_WINNER", blob)
        self.assertNotIn("development_corrected", blob)

    def test_mocked_one_call_hits_llama_only(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        calls: list[dict] = []

        def http_post(url, *, headers, json, timeout):
            del headers, timeout
            body = json
            calls.append({"url": url, "body": body})
            blob = __import__("json").dumps(body)
            self.assertNotIn("DEVELOPMENT_WINNER", blob)
            self.assertNotIn("development_corrected", blob)
            self.assertEqual(body["model"], "llama-3.3-70b-versatile")
            self.assertNotIn("reasoning_effort", body)
            return DummyResponse(
                {
                    "choices": [{"message": {"content": __import__("json").dumps(native)}}],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 22,
                        "total_tokens": 33,
                    },
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
        self.assertEqual(result["native_parse"], "PASS")
        self.assertEqual(result["gemini_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(SOURCE_BATCH_ID, "20260915T081205Z-2b732ddc")
        self.assertFalse(result["development_winner_exposed"])

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
        text = (REPO / "src/newsagent_v2/bench/writer_bakeoff/llama_33_audition.py").read_text(encoding="utf-8")
        for token in blocked:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


