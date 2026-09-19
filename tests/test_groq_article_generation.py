from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.input import build_article_input
from newsagent_v2.article.prompt import (
    article_output_json_schema,
    groq_schema_keyword_paths,
)
from newsagent_v2.article.runner import run_groq_article_generation
from newsagent_v2.providers.groq_article import (
    JSON_SCHEMA_NAME,
    build_article_chat_request_body,
)
from newsagent_v2.providers.groq_editorial import GROQ_MODEL_ID
from tests.fixtures.article_qa import CANDIDATE, JUDGMENT, clean_article
from tests.test_groq_editorial_benchmark import FakeResponse, SECRET


def _article_success_payload(article: dict, extra_usage=None) -> dict:
    usage = {
        "prompt_tokens": 200,
        "completion_tokens": 400,
        "total_tokens": 600,
    }
    if extra_usage:
        usage.update(extra_usage)
    return {
        "id": "chatcmpl-article-test-001",
        "choices": [{"message": {"content": json.dumps(article)}}],
        "usage": usage,
    }


class ArticleSchemaCompatibilityTests(unittest.TestCase):
    def test_provider_schema_has_no_unique_items(self) -> None:
        schema = article_output_json_schema()
        self.assertEqual(groq_schema_keyword_paths(schema), [])
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

    def test_provider_schema_omits_schema_version_constant(self) -> None:
        from newsagent_v2.article.batch_prompt import batch_output_json_schema

        article = article_output_json_schema()
        batch = batch_output_json_schema()
        self.assertNotIn("schema_version", article["properties"])
        self.assertNotIn("schema_version", article["required"])
        items = batch["properties"]["articles"]["items"]
        self.assertNotIn("schema_version", items["properties"])

    def test_recursive_walker_finds_nested_unique_items(self) -> None:
        schema = {
            "properties": {
                "claims": {
                    "items": {
                        "properties": {
                            "evidence_refs": {"uniqueItems": True},
                        }
                    }
                }
            }
        }
        paths = groq_schema_keyword_paths(schema)
        self.assertTrue(any("uniqueItems" in path for path in paths))


class ArticleRequestShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.article_input = build_article_input(CANDIDATE, JUDGMENT)
        self.body = build_article_chat_request_body(self.article_input)

    def test_model_and_reasoning(self) -> None:
        self.assertEqual(self.body["model"], GROQ_MODEL_ID)
        self.assertEqual(self.body["reasoning_effort"], "medium")
        self.assertIs(self.body["include_reasoning"], False)
        self.assertNotIn("reasoning_format", self.body)
        self.assertNotIn("tools", self.body)
        self.assertNotIn("tool_choice", self.body)

    def test_strict_article_schema(self) -> None:
        fmt = self.body["response_format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["json_schema"]["strict"])
        self.assertEqual(fmt["json_schema"]["name"], JSON_SCHEMA_NAME)
        schema = fmt["json_schema"]["schema"]
        self.assertNotIn("schema_version", schema["properties"])
        self.assertNotIn("schema_version", schema["required"])

    def test_user_payload_contains_event_and_no_browse(self) -> None:
        user = json.loads(self.body["messages"][1]["content"])
        self.assertEqual(user["article_input"]["event_id"], "event-syn-001")
        self.assertIs(user["article_input"]["browse"], False)
        self.assertIs(user["article_input"]["fetch_fulltext"], False)


class ArticleRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.article_input = build_article_input(CANDIDATE, JUDGMENT)
        self.article = clean_article()
        self.article["generation_notes"] = ""

    def test_missing_api_key_makes_no_http_request(self) -> None:
        calls: list = []

        def http_post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse(200, {})

        result = run_groq_article_generation(
            self.article_input,
            environ={},
            http_post=http_post,
        )
        self.assertEqual(calls, [])
        self.assertFalse(result["http_called"])
        self.assertFalse(result["success"])

    def test_mocked_success_runs_qa(self) -> None:
        payload = _article_success_payload(self.article)

        def http_post(*args, **kwargs):
            return FakeResponse(200, payload)

        result = run_groq_article_generation(
            self.article_input,
            environ={"GROQ_API_KEY": SECRET},
            http_post=http_post,
        )
        self.assertTrue(result["http_called"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["http_request_count"], 1)
        self.assertTrue(result["qa_result"]["qa_passed"])

    def test_secret_not_written_to_artifacts(self) -> None:
        payload = _article_success_payload(self.article)

        def http_post(*args, **kwargs):
            return FakeResponse(200, payload)

        with tempfile.TemporaryDirectory() as tmp:
            result = run_groq_article_generation(
                self.article_input,
                environ={"GROQ_API_KEY": SECRET},
                http_post=http_post,
                persist=True,
                persist_root=Path(tmp),
            )
            for path in Path(tmp).rglob("*.json"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(SECRET, text)
            self.assertIn("generation_input", result["artifact_paths"])


