from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.batch_runner import generate_make_articles
from newsagent_v2.article.enrich import (
    MIN_DISTINCT_FACTS,
    MIN_EXTRACTED_WORDS,
    STATUS_INSUFFICIENT,
    enrich_stories,
)
from newsagent_v2.article.input import build_article_input
from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.control.make import (
    DEFAULT_MAKE_COOLDOWN_SECONDS,
    MAKE_COOLDOWN_TEXT,
    execute_make,
    reset_make_guard,
)
from newsagent_v2.providers.groq_article import (
    BATCH_MAX_COMPLETION_TOKENS,
    build_top5_article_batch_request_body,
)
from newsagent_v2.providers.groq_editorial import (
    GROQ_CONTEXT_WINDOW,
    GROQ_MAX_OUTPUT_TOKENS,
    MAX_RETRY_AFTER_SECONDS,
    safe_chat_request_diagnostics,
    sanitize_provider_error,
)
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TOKEN_ENV, CHAT_ENV, load_telegram_config
from newsagent_v2.telegram.contract import ACK_MAKE_TEXT
from newsagent_v2.telegram.listener import handle_update, is_make_command
from tests.test_groq_editorial_benchmark import FakeResponse
from tests.test_top5_single_call import IDS, _article, _payload, _sufficient_row, RecordingPost

REPO = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (REPO / "tests" / "fixtures" / "make_batch_20260915" / "selected_events.json").read_text(
        encoding="utf-8"
    )
)
SECRET = "TEST_GROQ_LIVE_SHAPED_SECRET"
ENV = {"GROQ_API_KEY": SECRET}
TOKEN = "1234567890:AA-test-token-value-not-real"
CHAT = "-1001234567890"


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})


def _make_update(text: str = "/make") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 11, "is_bot": False, "first_name": "Alex"},
            "chat": {"id": int(CHAT), "type": "supergroup"},
            "text": text,
        },
    }


def _story_from_fixture(event_id: str, *, extracted: str = "") -> dict:
    info = FIXTURE["events"][event_id]
    evidence = []
    for index, url in enumerate(info["urls"]):
        summary = info["summaries"][index] if index < len(info["summaries"]) else ""
        item = {
            "source": info["sources"][index] if index < len(info["sources"]) else "Wire",
            "source_type": "newsroom",
            "source_role": "discovery",
            "source_authority": 0.8,
            "title": f"{event_id} title",
            "url": url,
            "published": "Mon, 14 Sep 2026 12:00:00 +0000",
            "summary": summary,
        }
        if extracted:
            item["extracted_text"] = extracted
            item["factual_snippets"] = [extracted]
        evidence.append(item)
    candidate = {
        "event_id": event_id,
        "representative_title": f"{event_id} title",
        "event_score": 1.0,
        "sources": info["sources"],
        "source_count": len(info["sources"]),
        "evidence": evidence,
    }
    return {
        "event_id": event_id,
        "article_input": build_article_input(candidate),
        "article_url": info["urls"][0],
        "source_count": len(info["sources"]),
    }


class ProviderErrorTelemetryTests(unittest.TestCase):
    def test_http_400_error_body_is_safely_captured(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS[:3]] + [
            _sufficient_row(IDS[3]),
            _sufficient_row(IDS[4]),
        ]
        payload = {
            "error": {
                "message": "invalid JSON schema for response_format: 'top5_article_batch_v1'",
                "type": "invalid_request_error",
                "code": "unsupported_keyword",
                "param": "response_format",
            },
            "id": "req_test_400",
        }
        poster = RecordingPost([FakeResponse(400, payload)])
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-400")
        self.assertEqual(len(poster.calls), 1)
        error = result["telemetry"]["provider_error"]
        self.assertEqual(error["http_status"], 400)
        self.assertEqual(error["provider_error_type"], "invalid_request_error")
        self.assertEqual(error["provider_error_code"], "unsupported_keyword")
        self.assertIn("invalid JSON schema", error["provider_error_message"])
        self.assertEqual(error["provider_error_param"], "response_format")
        blob = json.dumps(result["telemetry"])
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("Authorization", blob)

    def test_http_429_retry_after_captured_and_respected(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        sleeps: list[float] = []
        poster = RecordingPost(
            [
                FakeResponse(
                    429,
                    {"error": {"message": "Rate limit reached", "type": "rate_limit_exceeded", "code": "rate_limit"}},
                    {"Retry-After": "2", "x-request-id": "req_429"},
                ),
                FakeResponse(
                    429,
                    {"error": {"message": "Rate limit reached", "type": "rate_limit_exceeded"}},
                    {"Retry-After": "2"},
                ),
            ]
        )
        result = generate_make_articles(
            rows,
            environ=ENV,
            http_post=poster,
            sleep=sleeps.append,
            batch_id="batch-429",
        )
        self.assertEqual(len(poster.calls), 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(result["telemetry"]["retries"], 1)
        self.assertEqual(result["telemetry"]["http_status"], 429)
        self.assertEqual(result["telemetry"]["retry_after"], "2")
        self.assertEqual(result["telemetry"]["retry_slept_seconds"], 2.0)
        self.assertEqual(result["telemetry"]["provider_error"]["provider_error_type"], "rate_limit_exceeded")
        self.assertEqual(result["http_request_count"], 1)
        self.assertEqual(result["telemetry"]["editorial_ai_request_count"], 1)

    def test_retry_after_is_capped(self) -> None:
        sleeps: list[float] = []
        rows = [_sufficient_row(event_id) for event_id in IDS]
        poster = RecordingPost(
            [
                FakeResponse(429, {"error": {"message": "slow"}}, {"Retry-After": "120"}),
                FakeResponse(200, _payload([_article(event_id) for event_id in IDS])),
            ]
        )
        result = generate_make_articles(
            rows,
            environ=ENV,
            http_post=poster,
            sleep=sleeps.append,
            batch_id="batch-cap",
        )
        self.assertEqual(sleeps, [float(MAX_RETRY_AFTER_SECONDS)])
        self.assertEqual(result["telemetry"]["retries"], 1)
        self.assertEqual(result["telemetry"]["http_status"], 200)

    def test_no_per_story_fallback_after_batch_http_failure(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        poster = RecordingPost(
            [FakeResponse(400, {"error": {"message": "bad schema", "type": "invalid_request_error"}})]
        )
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-no-fallback")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(result["http_request_count"], 1)
        self.assertEqual(len(result["failures"]), 5)

    def test_image_requests_zero_when_editorial_http_fails(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        poster = RecordingPost([FakeResponse(400, {"error": {"message": "bad"}})])
        generated = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-img")
        images: list[str] = []
        batch = run_top5_batch(
            event_ids=IDS,
            stories=rows,
            image_fn=lambda job: images.append(job["event_id"])
            or {"success": True, "final_path": "x.png", "image_request_count": 1},
            provider_telemetry=generated["telemetry"],
        )
        self.assertEqual(images, [])
        self.assertEqual(batch["telemetry"]["image_request_count"], 0)
        self.assertEqual(batch["telemetry"]["editorial"]["http_status"], 400)
        self.assertIsNotNone(batch["telemetry"]["editorial"]["provider_error"])

    def test_sanitize_redacts_secrets_in_error_message(self) -> None:
        record = sanitize_provider_error(
            status_code=400,
            payload={"error": {"message": f"bad key {SECRET}", "type": "invalid_request_error"}},
            secrets=[SECRET],
            model="openai/gpt-oss-120b",
        )
        blob = json.dumps(record)
        self.assertNotIn(SECRET, blob)
        self.assertIn("[REDACTED]", blob)


class MakeCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        reset_make_guard(cooldown_seconds=60, clock=self.clock)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(Path(self.tmpdir.name))
        self.config = load_telegram_config({TOKEN_ENV: TOKEN, CHAT_ENV: CHAT})
        self.transport = RecordingTransport()
        self.client = TelegramTestClient(self.config, transport=self.transport)
        self.pipeline_calls = 0

    def tearDown(self) -> None:
        reset_make_guard(cooldown_seconds=0)
        self.tmpdir.cleanup()

    def _pipeline(self):
        def run():
            self.pipeline_calls += 1
            return {
                "ok": True,
                "stories": [],
                "selected_event_ids": IDS,
                "approval_cards": [],
                "completion_text": "done",
                "batch_run_id": "offline",
            }

        return run

    def test_duplicate_make_during_active_run_blocked(self) -> None:
        from newsagent_v2.control.make import MAKE_LOCK

        MAKE_LOCK.acquire()
        try:
            result = handle_update(
                _make_update("/make"),
                config=self.config,
                client=self.client,
                store=self.store,
                pipeline=self._pipeline(),
            )
            self.assertTrue(result.get("busy"))
            self.assertEqual(self.pipeline_calls, 0)
        finally:
            MAKE_LOCK.release()

    def test_duplicate_make_during_cooldown_blocked(self) -> None:
        first = execute_make(
            telegram_config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.assertTrue(first.get("acked"))
        self.assertEqual(self.pipeline_calls, 1)
        second = handle_update(
            _make_update("/make"),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.assertTrue(second.get("cooldown"))
        self.assertEqual(self.pipeline_calls, 1)
        texts = [call["kwargs"].get("json", {}).get("text") for call in self.transport.calls]
        self.assertIn(MAKE_COOLDOWN_TEXT, texts)
        self.assertNotIn(ACK_MAKE_TEXT, texts[-1:])

    def test_make_after_cooldown_allowed(self) -> None:
        execute_make(
            telegram_config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.clock.advance(61)
        third = execute_make(
            telegram_config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.assertTrue(third.get("acked"))
        self.assertEqual(self.pipeline_calls, 2)

    def test_make_at_bot_and_make_share_cooldown(self) -> None:
        self.assertTrue(is_make_command("/make@Newsagentbot"))
        handle_update(
            _make_update("/make@Newsagentbot"),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.assertEqual(self.pipeline_calls, 1)
        blocked = handle_update(
            _make_update("/make"),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.assertTrue(blocked.get("cooldown"))
        self.assertEqual(self.pipeline_calls, 1)

    def test_default_cooldown_is_60(self) -> None:
        reset_make_guard()
        self.assertEqual(DEFAULT_MAKE_COOLDOWN_SECONDS, 60)


class EvidenceAndSizeTests(unittest.TestCase):
    def test_evidence_sufficiency_thresholds_unchanged(self) -> None:
        self.assertEqual(MIN_EXTRACTED_WORDS, 80)
        self.assertEqual(MIN_DISTINCT_FACTS, 8)

    def test_event_004_and_005_fail_when_coindesk_blocked(self) -> None:
        def fetch(url: str):
            if "coindesk.com" in url:
                return 403, "text/html", b"<html>paywalled 13 trillion RLUSD</html>", url
            return 404, "", b"", url

        for event_id in ("event-004", "event-005"):
            row = enrich_stories([_story_from_fixture(event_id)], fetch=fetch)[0]
            metrics = row["evidence_sufficiency"]
            self.assertEqual(row["skip_reason"], STATUS_INSUFFICIENT)
            self.assertEqual(metrics["status"], STATUS_INSUFFICIENT)
            self.assertLess(metrics["extracted_evidence_words"], MIN_EXTRACTED_WORDS)
            self.assertLess(metrics["distinct_fact_count"], MIN_DISTINCT_FACTS)
            methods = [item.get("extraction_method") for item in row["article_input"]["evidence"]]
            self.assertTrue(any(str(item).startswith("fetch_blocked_403") for item in methods))
            self.assertFalse(any(item.get("extracted_text") for item in row["article_input"]["evidence"]))

    def test_serialized_request_size_is_under_context_window(self) -> None:
        extracted = ("Fact sentence with 2026 numbers $10.1 billion according to filings. " * 40)[:3500]
        stories = []
        for event_id in FIXTURE["sufficient"]:
            row = _story_from_fixture(event_id, extracted=extracted)
            if len(row["article_input"]["evidence"]) == 1:
                extra = dict(row["article_input"]["evidence"][0])
                extra["url"] = extra["url"] + "-second"
                extra["extracted_text"] = extracted
                row["article_input"]["evidence"].append(extra)
            row["evidence_sufficiency"] = {"status": "SUFFICIENT_EVIDENCE"}
            stories.append(row)
        body = build_top5_article_batch_request_body(batch_id="offline-size", stories=stories)
        diag = safe_chat_request_diagnostics(body)
        self.assertEqual(diag["max_completion_tokens"], BATCH_MAX_COMPLETION_TOKENS)
        self.assertEqual(BATCH_MAX_COMPLETION_TOKENS, 16384)
        self.assertEqual(diag["model_context_window"], GROQ_CONTEXT_WINDOW)
        self.assertEqual(diag["model_max_output_tokens"], GROQ_MAX_OUTPUT_TOKENS)
        self.assertLess(diag["estimated_serialized_tokens"] + BATCH_MAX_COMPLETION_TOKENS, GROQ_CONTEXT_WINDOW)
        self.assertGreater(diag["serialized_bytes"], 1000)
        self.assertNotIn("stories", diag)

    def test_wordpress_remains_disabled(self) -> None:
        result = wordpress_disabled_publish(story={})
        self.assertTrue(result["wordpress_disabled"])
        self.assertIsNone(result["url"])
        self.assertTrue(result["held"])


if __name__ == "__main__":
    unittest.main()


