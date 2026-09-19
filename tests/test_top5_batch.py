from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.batch.contract import TOP5_COUNT, BatchError
from newsagent_v2.batch.images import run_image_jobs
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.batch.select import select_top5_event_ids
from newsagent_v2.cluster import EventCluster
from newsagent_v2.models import NewsItem
from newsagent_v2.telegram.config import TelegramConfigError, load_telegram_config
from newsagent_v2.telegram.contract import KIND_BATCH_HEADER, LIVE_SEND_ENABLED, TelegramOutbound
from newsagent_v2.telegram.formatter import format_outbound_text
from newsagent_v2.telegram.batch import batch_header_payload, story_outbound
from newsagent_v2.telegram.__main__ import main as telegram_main
from newsagent_v2.batch.__main__ import LIVE_BATCH_COMMAND, main as batch_main

REPO = Path(__file__).resolve().parents[1]
TOKEN = "1234567890:AA-test-token-value-not-real"
CHAT = "-1001234567890"
PIXEL = REPO / "tests" / "fixtures" / "telegram" / "pixel.png"
IDS = [f"event-00{i}" for i in range(1, 6)]


def _item(event_id: str) -> NewsItem:
    return NewsItem(
        source=f"Wire-{event_id}",
        title=f"Headline for {event_id}",
        url=f"https://example.com/{event_id}",
        published="Mon, 14 Sep 2026 12:00:00 +0000",
        summary=f"Summary unique to {event_id} only.",
        source_role="primary_evidence",
    )


def _clusters() -> list[EventCluster]:
    clusters = []
    for index, event_id in enumerate(IDS, start=1):
        item = _item(event_id)
        cluster = EventCluster(event_id=event_id, representative=item, members=[item])
        cluster.event_score = float(10 - index)
        clusters.append(cluster)
    return clusters


def _article(event_id: str, *, body: str | None = None) -> dict:
    text = body if body is not None else f"Dek for {event_id}."
    return {
        "event_id": event_id,
        "headline": f"Headline {event_id}",
        "dek": text,
        "article_body": "x" * 400,
        "category": "market_move",
    }


def _story(event_id: str, *, fail_qa: bool = False) -> dict:
    article = _article(event_id, body="too short" if fail_qa else f"Dek for {event_id}.")
    if fail_qa:
        article["headline"] = ""
    return {
        "event_id": event_id,
        "article": article,
        "article_input": {
            "event_id": event_id,
            "evidence": [
                {
                    "url": f"https://example.com/{event_id}",
                    "title": f"Title {event_id}",
                    "summary": f"Summary unique to {event_id} only.",
                    "source": f"Wire-{event_id}",
                }
            ],
        },
        "source_count": 1,
        "category": "market_move",
    }


def _qa_fn(article, article_input, **kwargs):
    event_id = article_input.get("event_id")
    evidence = article_input.get("evidence") or []
    urls = [row.get("url") for row in evidence if isinstance(row, dict)]
    publishable = bool((article or {}).get("headline"))
    return {
        "event_id": event_id,
        "publishable": publishable,
        "qa_passed": publishable,
        "critical_failures": []
        if publishable
        else [{"code": "empty_headline", "message": "headline is empty", "module": "headline"}],
        "evidence_urls": urls,
        "article_event_id": (article or {}).get("event_id"),
    }


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._n = 0

    def __call__(self, *args, **kwargs):
        self._n += 1
        self.calls.append({"args": args, "kwargs": kwargs})

        class Response:
            status_code = 200

            def json(inner_self):
                return {"ok": True, "result": {"message_id": 1000 + len(self.calls)}}

        return Response()


class SelectTests(unittest.TestCase):
    def test_exactly_five_ranked_stories_preserve_order(self) -> None:
        ids = select_top5_event_ids(ranked_clusters=_clusters())
        self.assertEqual(ids, IDS)
        self.assertEqual(len(ids), TOP5_COUNT)

    def test_wrong_count_rejected(self) -> None:
        with self.assertRaises(BatchError) as ctx:
            select_top5_event_ids(event_ids=IDS + ["event-006"])
        self.assertEqual(ctx.exception.code, "top5_count")

    def test_explicit_ids_allow_fewer_than_five(self) -> None:
        ids = select_top5_event_ids(event_ids=IDS[:3])
        self.assertEqual(ids, IDS[:3])


class BatchRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        PIXEL.parent.mkdir(parents=True, exist_ok=True)
        if not PIXEL.is_file():
            PIXEL.write_bytes(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
                )
            )
        self.config = load_telegram_config(
            {
                "NEWSAGENT_V2_TELEGRAM_BOT_TOKEN": TOKEN,
                "NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID": CHAT,
            }
        )
        self.transport = RecordingTransport()
        self.qa_calls: list[tuple[str, list]] = []

    def _qa(self, article, article_input, **kwargs):
        result = _qa_fn(article, article_input, **kwargs)
        self.qa_calls.append((article_input.get("event_id"), result.get("evidence_urls")))
        return result

    def _image(self, job):
        if job["event_id"] == "event-005":
            return {"success": False, "reason": "image_failed", "image_request_count": 1}
        return {
            "success": True,
            "final_path": str(PIXEL),
            "image_request_count": 1,
        }

    def test_independent_qa_and_failure_isolation(self) -> None:
        stories = [_story(event_id) for event_id in IDS]
        stories[2] = _story("event-003", fail_qa=True)
        result = run_top5_batch(
            event_ids=IDS,
            stories=stories,
            qa_fn=self._qa,
            image_fn=self._image,
            telegram_config=self.config,
            telegram_transport=self.transport,
            live_telegram=False,
            persist=False,
            repo_root=REPO,
        )
        self.assertEqual(result["selected_event_ids"], IDS)
        self.assertEqual(len(self.qa_calls), 5)
        for event_id, urls in self.qa_calls:
            self.assertEqual(urls, [f"https://example.com/{event_id}"])
            self.assertTrue(all(event_id in (url or "") for url in urls))
        by_id = {row["event_id"]: row for row in result["stories"]}
        self.assertFalse(by_id["event-003"]["qa_publishable"])
        self.assertEqual(by_id["event-003"]["skip_reason"][:14], "empty_headline")
        self.assertTrue(by_id["event-001"]["qa_publishable"])
        self.assertTrue(by_id["event-002"]["qa_publishable"])
        self.assertTrue(by_id["event-004"]["qa_publishable"])
        self.assertTrue(by_id["event-001"]["deliverable"])
        self.assertFalse(by_id["event-003"]["deliverable"])
        self.assertFalse(by_id["event-005"]["deliverable"])
        self.assertEqual(by_id["event-005"]["skip_reason"], "image_failed")

        sends = result["telegram"]["sends"]
        story_sends = [item for item in sends if item.get("kind") != KIND_BATCH_HEADER]
        delivered = [item for item in story_sends if not item.get("skipped") and item.get("success")]
        self.assertEqual([item["event_id"] for item in delivered], ["event-001", "event-002", "event-004"])
        self.assertEqual(delivered[0]["story_index"], 1)
        self.assertEqual(delivered[-1]["story_index"], 4)
        skipped = [item for item in story_sends if item.get("skipped")]
        self.assertEqual({item["event_id"] for item in skipped}, {"event-003", "event-005"})
        self.assertTrue(any(item.get("kind") == KIND_BATCH_HEADER for item in sends))

        texts = [str(item.get("text") or "") for item in delivered]
        self.assertTrue(all("Story " in text for text in texts))
        self.assertIn("Story 1/5", texts[0])
        self.assertTrue(all("x" * 50 not in text for text in texts))
        self.assertTrue(all("article_body" not in text for text in texts))
        blob = json.dumps(result)
        self.assertNotIn(TOKEN, blob)
        self.assertFalse(LIVE_SEND_ENABLED)
        self.assertTrue(self.config.test_mode)
        self.assertTrue(result["telemetry"]["image_request_count"] >= 1)
        self.assertEqual(result["telemetry"]["selected_stories"], IDS)
        self.assertIn("event-001", result["telemetry"]["publishable_stories"])
        self.assertNotIn("event-003", [row["event_id"] for row in result["stories"] if row["deliverable"]])
        self.assertTrue(result["image_parallel_ready"])
        self.assertGreaterEqual(result["telegram"]["telegram_sends"], 4)

    def test_missing_image_skips_delivery(self) -> None:
        stories = [_story(event_id) for event_id in IDS]
        result = run_top5_batch(
            event_ids=IDS,
            stories=stories,
            qa_fn=_qa_fn,
            image_fn=lambda job: {"success": False, "reason": "missing_image", "image_request_count": 0},
            telegram_config=self.config,
            telegram_transport=self.transport,
            repo_root=REPO,
        )
        self.assertEqual(result["telegram"]["telegram_sends"], 0)
        self.assertTrue(all(not row["deliverable"] for row in result["stories"]))

    def test_production_telegram_impossible(self) -> None:
        from newsagent_v2.telegram.config import TelegramConfig

        with self.assertRaises(TelegramConfigError):
            TelegramConfig(bot_token=TOKEN, test_chat_id=CHAT, test_mode=False)

    def test_batch_telemetry_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stories = [_story(event_id) for event_id in IDS]
            result = run_top5_batch(
                event_ids=IDS,
                stories=stories,
                qa_fn=_qa_fn,
                image_fn=self._image,
                telegram_config=self.config,
                telegram_transport=self.transport,
                persist=True,
                persist_root=Path(tmp),
                repo_root=REPO,
            )
            telemetry = result["telemetry"]
            self.assertEqual(telemetry["selected_count"], 5)
            self.assertIn("total_elapsed_ms", telemetry)
            self.assertIn("ai_request_count", telemetry)
            self.assertIn("telegram_sends", telemetry)
            self.assertIn("retries", telemetry)
            self.assertIsNone(telemetry["provider_reported_cost_usd"])
            persisted = (Path(tmp) / result["batch_run_id"] / "telemetry.json").read_text(encoding="utf-8")
            self.assertNotIn(TOKEN, persisted)

    def test_editorial_hook_called_once(self) -> None:
        calls = []

        def editorial_fn(ids):
            calls.append(list(ids))
            return {"selected_event_ids": ids}

        stories = [_story(event_id) for event_id in IDS]
        run_top5_batch(
            event_ids=IDS,
            stories=stories,
            editorial_fn=editorial_fn,
            qa_fn=_qa_fn,
            image_fn=self._image,
            telegram_config=self.config,
            telegram_transport=self.transport,
            repo_root=REPO,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], IDS)

    def test_image_jobs_preserve_independence_when_parallel(self) -> None:
        jobs = [{"event_id": event_id} for event_id in IDS]

        def image_fn(job):
            return {"success": True, "event_id": job["event_id"], "final_path": str(PIXEL)}

        results = run_image_jobs(jobs, image_fn, parallel=True, max_workers=5)
        self.assertEqual([row["event_id"] for row in results], IDS)

    def test_cli_refuses_without_live_opt_in(self) -> None:
        code = batch_main([])
        self.assertEqual(code, 2)
        self.assertIn("--test-top5", LIVE_BATCH_COMMAND)
        self.assertEqual(telegram_main([]), 2)


class FormatterBatchTests(unittest.TestCase):
    def test_story_caption_has_slot_and_not_full_article(self) -> None:
        payload = story_outbound(
            event_id="event-001",
            headline="Bitcoin ETF outflows accelerate",
            dek="Investors pulled $449M in three days.",
            category="etf_product",
            source_count=2,
            image_path=None,
            story_index=1,
        )
        text = str(format_outbound_text(payload, caption=True)["text"])
        self.assertIn("[TEST] CoinNetwork V2 â€” Top 5", text)
        self.assertIn("Story 1/5", text)
        self.assertIn("Bitcoin ETF outflows accelerate", text)
        self.assertNotIn("article_body", text)
        header = str(format_outbound_text(batch_header_payload())["text"])
        self.assertEqual(header, "[TEST] CoinNetwork V2 â€” Top 5")

    def test_single_story_format_unchanged_without_slot(self) -> None:
        payload = TelegramOutbound(
            event_id="event-021",
            headline="Revolut reports customer data exposure",
            dek="A spoofed email was used.",
            mode="test",
        )
        text = str(format_outbound_text(payload)["text"])
        self.assertTrue(text.startswith("[TEST] CoinNetwork V2"))
        self.assertNotIn("Story ", text)


if __name__ == "__main__":
    unittest.main()


