from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

import requests

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import (
    BASE_URL,
    HARD_MAX_GENERATION_CALLS,
    HTTP_RETRY_TOTAL,
    KEY_ENV,
    KIMI_MODEL,
    REAL_INFERENCE_AUTHORIZED,
    BedrockMantleKimiWriterProvider,
    default_transport,
    load_bedrock_mantle_config,
    no_retry_session,
)
from newsagent_v2.article.writer.protocol import FrozenStoryPackage
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_KIMI_K25_ARTICLE_FIRST,
    EVENT_ID,
    KIMI_HARD_MAX_GENERATION_CALLS,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.kimi_safety import (
    assert_frozen_fixture_only,
    bakeoff_live_selects_kimi,
    live_make_can_select_kimi,
    run_kimi_safety_setup,
    run_simulated_guarded_generation,
)
from newsagent_v2.bench.writer_bakeoff.kimi_k25_audition import run_authorized_one_real_benchmark
from newsagent_v2.bench.writer_bakeoff.providers import next_writer_candidate
from newsagent_v2.control import live as live_mod

REPO = Path(__file__).resolve().parents[1]
FAKE_KEY = "test-kimi-secret-DO-NOT-LEAK-xyz789"


class DummyResponse:
    def __init__(self, status_code: int, payload: dict | None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class KimiK25SafetyTests(unittest.TestCase):
    def test_safety_setup_checks_presence_without_exposing_value(self) -> None:
        result = run_kimi_safety_setup(environ={KEY_ENV: FAKE_KEY})
        self.assertEqual(result["kimi_credential_present"], "YES")
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["real_kimi_calls"], 0)
        self.assertFalse(result["real_inference_authorized"])
        blob = json.dumps(result)
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn("Authorization", blob)
        missing = run_kimi_safety_setup(environ={})
        self.assertEqual(missing["kimi_credential_present"], "NO")
        self.assertEqual(missing["real_kimi_calls"], 0)

    def test_hard_cap_and_zero_retries(self) -> None:
        self.assertEqual(HARD_MAX_GENERATION_CALLS, 1)
        self.assertEqual(KIMI_HARD_MAX_GENERATION_CALLS, 1)
        self.assertEqual(HTTP_RETRY_TOTAL, 0)
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        adapter = no_retry_session().get_adapter("https://example.invalid")
        self.assertEqual(getattr(adapter.max_retries, "total", adapter.max_retries), 0)

    def test_default_transport_cannot_reach_network(self) -> None:
        with self.assertRaises(Exception):
            default_transport(
                f"{BASE_URL}/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {FAKE_KEY}"},
                json_body={"model": KIMI_MODEL},
                timeout=1,
            )

    def test_wrong_authorization_makes_zero_real_calls(self) -> None:
        result = run_authorized_one_real_benchmark(
            authorization="not-the-one-shot-token",
            environ={KEY_ENV: FAKE_KEY},
            persist=False,
        )
        self.assertEqual(result["real_kimi_calls"], 0)
        self.assertEqual(result["generation_calls"], 0)
        self.assertEqual(result["stage"], "authorization")
        self.assertFalse(result["make_invoked"])
        self.assertNotIn(FAKE_KEY, json.dumps(result, default=str))

    def test_generate_without_transport_makes_zero_calls(self) -> None:
        config = load_bedrock_mantle_config({KEY_ENV: FAKE_KEY})
        provider = BedrockMantleKimiWriterProvider(config)
        story = FrozenStoryPackage(event_id=EVENT_ID, article_input={"event_id": EVENT_ID})
        result = provider.generate(story)
        self.assertEqual(result["http"]["real_http_attempted"], False)
        self.assertEqual(result["ledger"]["generation_calls"], 0)
        self.assertEqual(result["http"]["error_class"], "real_http_blocked")
        self.assertNotIn(FAKE_KEY, json.dumps(result, default=str))

    def test_build_request_omits_secrets_and_uses_frozen_event(self) -> None:
        config = load_bedrock_mantle_config({KEY_ENV: FAKE_KEY})
        provider = BedrockMantleKimiWriterProvider(config)
        story = FrozenStoryPackage(event_id=EVENT_ID, article_input={"event_id": EVENT_ID})
        body = provider.build_request(
            story,
            compact={"event_id": EVENT_ID, "evidence_units": [{"evidence_id": "event-005-e01"}]},
        )
        blob = json.dumps(body)
        self.assertEqual(body["model"], KIMI_MODEL)
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn("Authorization", blob)
        self.assertFalse(json.loads(body["messages"][1]["content"])["requirements"]["repair_call"])

    def _status_run(self, status: int) -> tuple[list[int], dict]:
        calls: list[int] = []

        def transport(url, *, method, headers, json_body, timeout):
            del url, method, timeout, json_body
            self.assertNotIn("Authorization", headers)
            calls.append(status)
            return DummyResponse(status, {"error": {"message": f"simulated {status}"}})

        result = run_simulated_guarded_generation(
            environ={KEY_ENV: FAKE_KEY},
            transport=transport,
            extra_generate_attempts=1,
            run_qa=False,
        )
        return calls, result

    def test_simulated_401_stops_without_retry(self) -> None:
        calls, result = self._status_run(401)
        self.assertEqual(calls, [401])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["repair_calls"], 0)
        self.assertEqual(result["fallback_calls"], 0)
        self.assertEqual(result["http_status"], 401)
        self.assertEqual(result["extra_generate_blocked"], 1)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["real_kimi_calls"], 0)
        self.assertEqual(result["secret_exposure_check"], "PASS")

    def test_simulated_429_stops_without_retry(self) -> None:
        calls, result = self._status_run(429)
        self.assertEqual(calls, [429])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["extra_generate_blocked"], 1)

    def test_simulated_503_stops_without_retry(self) -> None:
        calls, result = self._status_run(503)
        self.assertEqual(calls, [503])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(result["extra_generate_blocked"], 1)

    def test_simulated_timeout_stops_without_retry(self) -> None:
        calls: list[str] = []

        def transport(url, *, method, headers, json_body, timeout):
            del url, method, headers, json_body, timeout
            calls.append("timeout")
            raise requests.Timeout("simulated timeout")

        result = run_simulated_guarded_generation(
            environ={KEY_ENV: FAKE_KEY},
            transport=transport,
            extra_generate_attempts=1,
            run_qa=False,
        )
        self.assertEqual(calls, ["timeout"])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["error_class"], "timeout")
        self.assertEqual(result["extra_generate_blocked"], 1)
        self.assertEqual(result["real_kimi_calls"], 0)

    def test_qa_failure_cannot_trigger_another_generation(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        calls: list[str] = []

        def transport(url, *, method, headers, json_body, timeout):
            del url, timeout
            self.assertEqual(method, "POST")
            self.assertNotIn("Authorization", headers)
            self.assertEqual(json_body["model"], KIMI_MODEL)
            calls.append("post")
            return DummyResponse(
                200,
                {
                    "choices": [{"message": {"content": json.dumps(native)}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
                },
            )

        result = run_simulated_guarded_generation(
            environ={KEY_ENV: FAKE_KEY},
            transport=transport,
            extra_generate_attempts=1,
            run_qa=True,
        )
        self.assertEqual(calls, ["post"])
        self.assertEqual(result["generation_calls"], 1)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(result["repair_calls"], 0)
        self.assertEqual(result["fallback_calls"], 0)
        self.assertFalse(result["qa_publishable"])
        self.assertEqual(result["extra_generate_blocked"], 1)
        self.assertEqual(result["native_parse"], "PASS")
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(result["secret_exposure_check"], "PASS")
        self.assertNotIn(FAKE_KEY, json.dumps(result, default=str))
        self.assertEqual(SOURCE_BATCH_ID, "20260915T081205Z-2b732ddc")

    def test_live_make_cannot_select_kimi(self) -> None:
        self.assertFalse(live_make_can_select_kimi())
        self.assertFalse(bakeoff_live_selects_kimi())
        self.assertEqual(next_writer_candidate()["model"], "gemini-3.6-flash")
        self.assertNotEqual(next_writer_candidate()["candidate_id"], CANDIDATE_KIMI_K25_ARTICLE_FIRST)
        live_src = inspect.getsource(live_mod)
        self.assertNotIn("kimi", live_src.lower())
        self.assertNotIn("bedrock", live_src.lower())
        self.assertNotIn("moonshot", live_src.lower())
        self.assertNotIn("writer_bakeoff", live_src)

    def test_frozen_fixture_lock(self) -> None:
        locked = assert_frozen_fixture_only()
        self.assertTrue(locked.as_posix().endswith("benchmarks/writer_bakeoff/event-005"))
        with self.assertRaises(ValueError):
            assert_frozen_fixture_only(REPO / "benchmarks" / "writer_bakeoff")

    def test_isolation_tokens_absent(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        for rel in (
            "src/newsagent_v2/article/writer/bedrock_mantle.py",
            "src/newsagent_v2/bench/writer_bakeoff/kimi_safety.py",
            "src/newsagent_v2/bench/writer_bakeoff/kimi_k25_audition.py",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            for token in blocked:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


