from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.article.writer.schema import gemini_ledger_first_schema
from newsagent_v2.bench.writer_bakeoff.contract import (
    EVENT_ID,
    GEMINI_38_TEXT_MODEL,
    GEMINI_NEXT_TEXT_MODEL,
)
from newsagent_v2.bench.writer_bakeoff.gemini_ledger_first_audition import run_audition
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.bench.writer_bakeoff.providers import gemini_ledger_first_request, next_writer_candidate
from newsagent_v2.image.providers.gemini import generate_content_url

REPO = Path(__file__).resolve().parents[1]


class DummyResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class GeminiLedgerFirstAuditionTests(unittest.TestCase):
    def test_missing_key_makes_zero_calls(self) -> None:
        result = run_audition(environ={}, persist=False)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["exact_model"], GEMINI_NEXT_TEXT_MODEL)
        self.assertNotEqual(result["exact_model"], GEMINI_38_TEXT_MODEL)
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["groq_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertEqual(result["qa_unchanged"], "YES")
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        self.assertFalse(live_make_can_select_kimi())

    def test_schema_omits_claims_and_uses_gemini_3_6(self) -> None:
        self.assertEqual(GEMINI_NEXT_TEXT_MODEL, "gemini-3.6-flash")
        self.assertEqual(next_writer_candidate()["model"], "gemini-3.6-flash")
        schema = gemini_ledger_first_schema()
        self.assertNotIn("claims", schema.get("properties") or {})
        self.assertNotIn("quotes", schema.get("properties") or {})
        self.assertNotIn("paragraph_maps", schema.get("properties") or {})
        self.assertIn("/models/gemini-3.6-flash:generateContent", generate_content_url(GEMINI_NEXT_TEXT_MODEL))
        self.assertNotIn("gemini-3.8-flash", generate_content_url(GEMINI_NEXT_TEXT_MODEL))

    def test_mocked_one_call_hits_gemini_3_6_ledger_first_only(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        native.pop("claims", None)
        native.pop("quotes", None)
        native.pop("paragraph_maps", None)
        calls: list[str] = []

        def transport(url: str, headers: dict, json_body: dict, timeout: int):
            del headers, timeout
            calls.append(url)
            props = ((json_body.get("generationConfig") or {}).get("responseSchema") or {}).get("properties") or {}
            self.assertNotIn("claims", props)
            self.assertNotIn("quotes", props)
            blob = json.dumps(json_body)
            self.assertIn("closed factual ledger", blob)
            self.assertNotIn("DEVELOPMENT_WINNER", blob)
            self.assertNotIn("gemini-3.8-flash", url)
            return DummyResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": json.dumps(native)}]}}],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "totalTokenCount": 30},
                }
            )

        result = run_audition(
            environ={"NEWSAGENT_V2_GEMINI_API_KEY": "test-gemini-key"},
            transport=transport,
            persist=False,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["exact_model"], "gemini-3.6-flash")
        self.assertEqual(result["native_parse"], "PASS")
        self.assertEqual(result["normalization"], "PASS")
        self.assertEqual(result["kimi_calls"], 0)
        self.assertEqual(result["groq_calls"], 0)
        self.assertEqual(result["qwen_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertTrue(result["evidence_claim_ledger_unchanged"])
        self.assertTrue(result["quote_ledger_unchanged"])


if __name__ == "__main__":
    unittest.main()


