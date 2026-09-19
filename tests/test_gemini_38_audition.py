from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.bench.writer_bakeoff.contract import (
    EVENT_ID,
    GEMINI_38_TEXT_MODEL,
    GEMINI_NEXT_TEXT_MODEL,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GEMINI_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.execute import _gemini_text
from newsagent_v2.bench.writer_bakeoff.gemini_38_audition import run_audition
from newsagent_v2.bench.writer_bakeoff.providers import next_writer_candidate
from newsagent_v2.image.providers.gemini import generate_content_url

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class Gemini38AuditionTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_audition(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], GEMINI_38_TEXT_MODEL)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["groq_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertTrue(result["qa_unchanged"])
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_next_writer_remains_gemini_3_6(self) -> None:
        self.assertEqual(GEMINI_NEXT_TEXT_MODEL, "gemini-3.6-flash")
        self.assertEqual(next_writer_candidate()["model"], "gemini-3.6-flash")
        self.assertIn("/models/gemini-3.8-flash:generateContent", generate_content_url(GEMINI_38_TEXT_MODEL))

    def test_thought_parts_are_skipped(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "internal reasoning"},
                            {"text": json.dumps(native)},
                        ]
                    }
                }
            ]
        }
        text = _gemini_text(payload)
        self.assertIsNotNone(text)
        self.assertNotIn("internal reasoning", text or "")
        self.assertIn("headline", text or "")

    def test_mocked_one_call_hits_gemini_3_8_only(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        calls: list[str] = []

        def transport(url: str, headers: dict, json_body: dict, timeout: int):
            del headers, timeout
            calls.append(url)
            blob = json.dumps(json_body)
            self.assertNotIn("DEVELOPMENT_WINNER", blob)
            self.assertNotIn("development_corrected", blob)
            return DummyResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": json.dumps(native)}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 11,
                        "candidatesTokenCount": 22,
                        "totalTokenCount": 33,
                    },
                }
            )

        result = run_audition(
            environ={GEMINI_KEY_ENV: "test-not-a-real-key"},
            transport=transport,
            persist=False,
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("/models/gemini-3.8-flash:generateContent", calls[0])
        self.assertNotIn("gemini-3.6-flash", calls[0])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["native_parse"], "PASS")
        self.assertEqual(result["groq_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(SOURCE_BATCH_ID, "20260915T081205Z-2b732ddc")
        self.assertFalse(result["development_winner_exposed"])

    def test_isolation_tokens_absent(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        text = (REPO / "src/newsagent_v2/bench/writer_bakeoff/gemini_38_audition.py").read_text(encoding="utf-8")
        for token in blocked:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


