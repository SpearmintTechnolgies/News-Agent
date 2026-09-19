from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.benchmark.input import build_editorial_input
from newsagent_v2.benchmark.runner import (
    persist_run_artifacts,
    run_groq_editorial_benchmark,
)
from newsagent_v2.providers.groq_editorial import (
    DEFAULT_REASONING_EFFORT,
    GROQ_CHAT_COMPLETIONS_URL,
    GROQ_MODEL_ID,
    build_chat_request_body,
    post_chat_completion,
)
from tests.test_editorial_benchmark import make_fixture, valid_output


SECRET = "TEST_GROQ_SECRET"


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload=None,
        headers=None,
        *,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._payload


def _benchmark():
    ranking, clusters = make_fixture(15)
    return build_editorial_input(ranking, clusters)


def _groq_success_payload(editorial: dict, extra_usage=None) -> dict:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }
    if extra_usage:
        usage.update(extra_usage)
    return {
        "id": "chatcmpl-test-001",
        "choices": [
            {
                "message": {
                    "content": json.dumps(editorial),
                }
            }
        ],
        "usage": usage,
    }


class GroqRequestShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.benchmark = _benchmark()
        self.body = build_chat_request_body(self.benchmark)

    def test_correct_model_id(self) -> None:
        self.assertEqual(self.body["model"], GROQ_MODEL_ID)
        self.assertEqual(self.body["model"], "openai/gpt-oss-120b")

    def test_reasoning_effort_medium(self) -> None:
        self.assertEqual(self.body["reasoning_effort"], "medium")
        self.assertEqual(self.body["reasoning_effort"], DEFAULT_REASONING_EFFORT)

    def test_include_reasoning_false_and_no_reasoning_format(self) -> None:
        self.assertIs(self.body["include_reasoning"], False)
        self.assertNotIn("reasoning_format", self.body)

    def test_prompt_requires_symmetric_same_event(self) -> None:
        from newsagent_v2.benchmark.prompt import SYSTEM_PROMPT

        folded = " ".join(SYSTEM_PROMPT.split()).lower()
        self.assertIn("if a lists b in same_event_as, b must list a", folded)
        self.assertIn("follow-up developments", folded)

    def test_tools_not_enabled(self) -> None:
        self.assertNotIn("tools", self.body)
        self.assertNotIn("tool_choice", self.body)

    def test_judgments_locked_to_15_for_current_benchmark(self) -> None:
        judgments = self.body["response_format"]["json_schema"]["schema"][
            "properties"
        ]["judgments"]
        self.assertEqual(judgments["minItems"], 15)
        self.assertEqual(judgments["maxItems"], 15)

    def test_judgments_bounds_match_synthetic_candidate_count(self) -> None:
        ranking, clusters = make_fixture(8)
        payload = build_editorial_input(ranking, clusters, top_n=8)
        body = build_chat_request_body(payload)
        judgments = body["response_format"]["json_schema"]["schema"][
            "properties"
        ]["judgments"]
        self.assertEqual(payload["candidate_count"], 8)
        self.assertEqual(judgments["minItems"], 8)
        self.assertEqual(judgments["maxItems"], 8)

    def test_all_15_candidates_in_request(self) -> None:
        user = self.body["messages"][1]["content"]
        for candidate in self.benchmark["candidates"]:
            self.assertIn(candidate["event_id"], user)
        self.assertEqual(self.benchmark["candidate_count"], 15)

    def test_structured_output_schema_attached(self) -> None:
        fmt = self.body["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        schema_wrap = fmt["json_schema"]
        self.assertTrue(schema_wrap["strict"])
        schema = schema_wrap["schema"]
        self.assertIn("selected_event_ids", schema["properties"])
        self.assertIn("judgments", schema["properties"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "editorial-output-v1")

    def test_provider_schema_has_no_unique_items_keyword(self) -> None:
        schema = self.body["response_format"]["json_schema"]["schema"]
        found: list[str] = []

        def walk(node, path: str) -> None:
            if isinstance(node, dict):
                if "uniqueItems" in node:
                    found.append(path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{path}[{i}]")

        walk(schema, "$")
        self.assertEqual(found, [])

    def test_selected_event_ids_bounds(self) -> None:
        selected = self.body["response_format"]["json_schema"]["schema"][
            "properties"
        ]["selected_event_ids"]
        self.assertEqual(selected["minItems"], 5)
        self.assertEqual(selected["maxItems"], 5)
        self.assertNotIn("uniqueItems", selected)


class GroqAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.benchmark = _benchmark()
        self.valid = valid_output(self.benchmark)

    def test_missing_api_key_makes_no_http_request(self) -> None:
        calls: list = []

        def http_post(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("HTTP must not be called")

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={},
            http_post=http_post,
        )
        self.assertFalse(result["success"])
        self.assertFalse(result["http_called"])
        self.assertEqual(calls, [])
        self.assertIn("GROQ_API_KEY is not set", result["failure_reason"])

        http_result_calls: list = []

        def http_post_direct(*args, **kwargs):
            http_result_calls.append(1)
            raise AssertionError("HTTP must not be called")

        with self.assertRaises(Exception):
            post_chat_completion(
                build_chat_request_body(self.benchmark),
                api_key=None,
                http_post=http_post_direct,
            )
        self.assertEqual(http_result_calls, [])

    def test_successful_mocked_response_parses_and_validates(self) -> None:
        calls: list = []

        def http_post(url, *, headers, json, timeout):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse(200, _groq_success_payload(self.valid))

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={"GROQ_API_KEY": SECRET},
            http_post=http_post,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], GROQ_CHAT_COMPLETIONS_URL)
        self.assertEqual(calls[0]["json"]["model"], GROQ_MODEL_ID)
        self.assertTrue(result["http_called"])
        self.assertTrue(result["validator_passed"])
        self.assertEqual(result["validator_errors"], [])
        self.assertEqual(
            result["parsed_output"]["selected_event_ids"],
            self.valid["selected_event_ids"],
        )
        self.assertTrue(result["success"])

    def test_invalid_editorial_output_is_validator_failure(self) -> None:
        invalid = json.loads(json.dumps(self.valid))
        invalid["selected_event_ids"] = invalid["selected_event_ids"][:4]

        def http_post(url, *, headers, json, timeout):
            return FakeResponse(200, _groq_success_payload(invalid))

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={"GROQ_API_KEY": SECRET},
            http_post=http_post,
        )
        self.assertTrue(result["http_called"])
        self.assertFalse(result["validator_passed"])
        self.assertGreater(len(result["validator_errors"]), 0)
        self.assertFalse(result["success"])
        self.assertTrue(
            any("wrong Top-5 count" in e for e in result["validator_errors"])
        )

    def test_token_usage_telemetry_captured(self) -> None:
        def http_post(url, *, headers, json, timeout):
            return FakeResponse(
                200,
                _groq_success_payload(
                    self.valid,
                    extra_usage={
                        "prompt_tokens": 111,
                        "completion_tokens": 222,
                        "total_tokens": 333,
                    },
                ),
            )

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={"GROQ_API_KEY": SECRET},
            http_post=http_post,
        )
        telemetry = result["telemetry"]
        self.assertEqual(telemetry["prompt_tokens"], 111)
        self.assertEqual(telemetry["completion_tokens"], 222)
        self.assertEqual(telemetry["total_tokens"], 333)
        self.assertEqual(telemetry["provider_request_id"], "chatcmpl-test-001")
        self.assertIsNone(telemetry["provider_reported_cost_usd"])
        self.assertIn("diagnostic", telemetry["cost_notes"])

    def test_secret_not_written_to_telemetry_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_groq_editorial_benchmark(
                self.benchmark,
                environ={"GROQ_API_KEY": SECRET},
                http_post=lambda url, **kwargs: FakeResponse(
                    200, _groq_success_payload(self.valid)
                ),
                persist=True,
                persist_root=Path(tmp),
            )
            blob = json.dumps(result["telemetry"])
            self.assertNotIn(SECRET, blob)
            self.assertNotIn("Bearer " + SECRET, blob)
            paths = result["artifact_paths"]
            self.assertIsNotNone(paths)
            for path in paths.values():
                text = Path(path).read_text(encoding="utf-8")
                self.assertNotIn(SECRET, text)
                self.assertNotIn("Authorization", text)

    def test_retry_at_most_once_for_transient_failure(self) -> None:
        calls: list = []
        sleeps: list = []

        def http_post(url, *, headers, json, timeout):
            calls.append(1)
            if len(calls) == 1:
                return FakeResponse(429, {"error": "rate"}, {"Retry-After": "2"})
            return FakeResponse(200, _groq_success_payload(self.valid))

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={"GROQ_API_KEY": SECRET},
            http_post=http_post,
            sleep=sleeps.append,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(sleeps, [2.0])
        self.assertTrue(result["success"])

        calls.clear()
        sleeps.clear()

        def always_503(url, *, headers, json, timeout):
            calls.append(1)
            return FakeResponse(503, {"error": "busy"}, {"Retry-After": "1"})

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={"GROQ_API_KEY": SECRET},
            http_post=always_503,
            sleep=sleeps.append,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["retry_count"], 1)
        self.assertFalse(result["success"])

    def test_non_transient_4xx_does_not_retry(self) -> None:
        calls: list = []

        def http_post(url, *, headers, json, timeout):
            calls.append(1)
            return FakeResponse(400, {"error": "bad request"})

        result = run_groq_editorial_benchmark(
            self.benchmark,
            environ={"GROQ_API_KEY": SECRET},
            http_post=http_post,
            sleep=lambda _s: calls.append("slept"),
        )
        self.assertEqual(calls, [1])
        self.assertEqual(result["retry_count"], 0)
        self.assertFalse(result["success"])
        self.assertEqual(result["http_status"], 400)

    def test_artifacts_persisted_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_groq_editorial_benchmark(
                self.benchmark,
                environ={"GROQ_API_KEY": SECRET},
                http_post=lambda url, **kwargs: FakeResponse(
                    200, _groq_success_payload(self.valid)
                ),
                persist=True,
                persist_root=Path(tmp),
            )
            paths = result["artifact_paths"]
            self.assertTrue(Path(paths["request_metadata"]).is_file())
            self.assertTrue(Path(paths["raw_provider_response"]).is_file())
            self.assertTrue(Path(paths["parsed_editorial_output"]).is_file())
            self.assertTrue(Path(paths["validation"]).is_file())
            self.assertTrue(Path(paths["telemetry"]).is_file())
            meta = json.loads(Path(paths["request_metadata"]).read_text(encoding="utf-8"))
            self.assertFalse(meta["headers_included"])
            self.assertEqual(meta["body"]["model"], GROQ_MODEL_ID)
            parsed = json.loads(
                Path(paths["parsed_editorial_output"]).read_text(encoding="utf-8")
            )
            self.assertEqual(parsed["schema_version"], "editorial-output-v1")
            validation = json.loads(Path(paths["validation"]).read_text(encoding="utf-8"))
            self.assertTrue(validation["passed"])


class PersistHelperTests(unittest.TestCase):
    def test_persist_helper_redacts_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = persist_run_artifacts(
                Path(tmp) / "run",
                request_metadata={"note": SECRET},
                raw_response={"echo": SECRET},
                parsed_output={"ok": True},
                validation={"passed": True},
                telemetry={"token": SECRET},
                secrets=[SECRET],
            )
            for path in paths.values():
                self.assertNotIn(SECRET, Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


