from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from newsagent_v2.approval.callbacks import handle_callback
from newsagent_v2.approval.store import (
    STATE_APPROVED,
    STATE_AWAITING_APPROVAL,
    STATE_PUBLISHED,
    STATE_REJECTED,
    ApprovalStore,
)
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.batch.images import IMAGE_MAX_CONCURRENCY, run_image_jobs
from newsagent_v2.batch.select import select_top5_event_ids
from newsagent_v2.cluster import EventCluster
from newsagent_v2.control.__main__ import LISTEN_COMMAND, main as control_main
from newsagent_v2.control.make import MAKE_LOCK, execute_make, run_make_generation, reset_make_guard
from newsagent_v2.models import NewsItem
from newsagent_v2.telegram.cards import approval_keyboard, callback_data, parse_callback_data
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TOKEN_ENV, CHAT_ENV, load_telegram_config
from newsagent_v2.telegram.contract import ACK_MAKE_TEXT, BUSY_MAKE_TEXT, CALLBACK_DATA_LIMIT, LIVE_SEND_ENABLED
from newsagent_v2.telegram.listener import handle_update, is_make_command, run_listen_loop
from newsagent_v2.wordpress.adapter import WordPressPublishError, publish_frozen_story, sanitize_wp_error
from newsagent_v2.wordpress.config import WordPressConfig, WordPressConfigError, load_wordpress_config

REPO = Path(__file__).resolve().parents[1]
TOKEN = "1234567890:AA-test-token-value-not-real"
CHAT = "-1001234567890"
IDS = [f"event-00{i}" for i in range(1, 6)]
PIXEL = REPO / "tests" / "fixtures" / "telegram" / "pixel.png"


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload or {"ok": True, "result": {"message_id": 1}}

    def json(self):
        return self._payload


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.n = 0

    def __call__(self, *args, **kwargs):
        self.n += 1
        self.calls.append({"args": args, "kwargs": kwargs, "method": kwargs.get("method")})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 200 + self.n}})


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
    out = []
    for index, event_id in enumerate(IDS, start=1):
        item = _item(event_id)
        cluster = EventCluster(event_id=event_id, representative=item, members=[item])
        cluster.event_score = float(10 - index)
        out.append(cluster)
    return out


def _article(event_id: str, *, fail: bool = False) -> dict:
    return {
        "event_id": event_id,
        "headline": "" if fail else f"Headline {event_id}",
        "dek": f"Summary one for {event_id}. Summary two.",
        "article_body": "body " * 80,
        "slug": f"headline-{event_id}",
        "seo_title": f"SEO {event_id}",
        "meta_description": f"Meta {event_id}",
        "category": "market_move",
    }


def _stories(*, fail_qa: str | None = None) -> list[dict]:
    rows = []
    for event_id in IDS:
        rows.append(
            {
                "event_id": event_id,
                "article": _article(event_id, fail=event_id == fail_qa),
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
            }
        )
    return rows


def _qa(article, article_input, **kwargs):
    ok = bool((article or {}).get("headline"))
    return {
        "event_id": article_input.get("event_id"),
        "publishable": ok,
        "qa_passed": ok,
        "critical_failures": []
        if ok
        else [{"code": "empty_headline", "message": "headline is empty", "module": "headline"}],
    }


def _ensure_pixel() -> None:
    PIXEL.parent.mkdir(parents=True, exist_ok=True)
    if not PIXEL.is_file():
        PIXEL.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
            )
        )


class MakeListenerTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pixel()
        reset_make_guard(cooldown_seconds=0)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(Path(self.tmpdir.name))
        self.config = load_telegram_config({TOKEN_ENV: TOKEN, CHAT_ENV: CHAT})
        self.transport = RecordingTransport()
        self.client = TelegramTestClient(self.config, transport=self.transport)
        self.editorial_calls = []

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _image(self, job):
        if job["event_id"] == "event-005":
            return {"success": False, "reason": "image_failed", "image_request_count": 1}
        return {"success": True, "final_path": str(PIXEL), "image_request_count": 1}

    def _content(self, stories):
        self.editorial_calls.append([row["event_id"] for row in stories])
        return {row["event_id"]: row["article"] for row in stories}

    def _pipeline(self, *, fail_qa: str | None = "event-003"):
        stories = _stories(fail_qa=fail_qa)
        return lambda: run_make_generation(
            event_ids=IDS,
            ranked_clusters=_clusters(),
            stories=stories,
            editorial_fn=lambda ids: {"selected_event_ids": ids},
            content_fn=self._content,
            qa_fn=_qa,
            image_fn=self._image,
            store=self.store,
            telegram_config=self.config,
            telegram_client=self.client,
            parallel_images=True,
        )

    def _make_update(self, *, user_id: int, chat_id: str = CHAT, text: str = "/make"):
        return {
            "update_id": user_id,
            "message": {
                "message_id": 1,
                "from": {"id": user_id, "is_bot": False, "first_name": "Alex"},
                "chat": {"id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id, "type": "supergroup"},
                "text": text,
            },
        }

    def test_make_from_arbitrary_members_and_wrong_chat_ignored(self) -> None:
        pipeline = self._pipeline()
        first = handle_update(
            self._make_update(user_id=11),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=pipeline,
        )
        self.assertTrue(first.get("acked") or first.get("ok"))
        texts = [call["kwargs"].get("json", {}).get("text") for call in self.transport.calls if call["kwargs"].get("json")]
        self.assertEqual(texts[0], ACK_MAKE_TEXT)
        other = handle_update(
            self._make_update(user_id=99, chat_id="-199999"),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=pipeline,
        )
        self.assertTrue(other.get("ignored"))
        self.assertEqual(other.get("reason"), "wrong_chat")
        self.assertTrue(is_make_command("/make@NewsAgentV2Bot"))

    def test_duplicate_make_blocked(self) -> None:
        MAKE_LOCK.acquire()
        try:
            result = handle_update(
                self._make_update(user_id=22),
                config=self.config,
                client=self.client,
                store=self.store,
                pipeline=self._pipeline(),
            )
            self.assertTrue(result.get("busy"))
            sent = [call["kwargs"].get("json", {}).get("text") for call in self.transport.calls]
            self.assertIn(BUSY_MAKE_TEXT, sent)
        finally:
            MAKE_LOCK.release()

    def test_ranking_isolation_cards_and_failures(self) -> None:
        result = handle_update(
            self._make_update(user_id=33, text="/make extra"),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        self.assertEqual(select_top5_event_ids(ranked_clusters=_clusters()), IDS)
        self.assertEqual(result["selected_event_ids"], IDS)
        self.assertEqual(len(self.editorial_calls), 1)
        by_id = {row["event_id"]: row for row in result["stories"]}
        self.assertFalse(by_id["event-003"]["deliverable"])
        self.assertFalse(by_id["event-005"]["deliverable"])
        self.assertTrue(by_id["event-001"]["deliverable"])
        cards = result["approval_cards"]
        self.assertEqual([row["event_id"] for row in cards], ["event-001", "event-002", "event-004"])
        photo_calls = [call for call in self.transport.calls if call["kwargs"].get("method") == "sendPhoto"]
        self.assertEqual(len(photo_calls), 3)
        markup = photo_calls[0]["kwargs"]["json"]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "âœ… APPROVE")
        data = markup["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data), CALLBACK_DATA_LIMIT)
        self.assertTrue(data.startswith("ap:"))
        self.assertIn("âš ï¸ V2 PARTIAL â€” 3/5 READY", result["completion_text"])
        story = self.store.read_story(result["batch_id"], "event-001")
        self.assertEqual(story["state"], STATE_AWAITING_APPROVAL)
        blob = str(result)
        self.assertNotIn(TOKEN, blob)

    def test_approve_once_reject_and_wp_failure(self) -> None:
        gen = handle_update(
            self._make_update(user_id=44),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        batch_id = gen["batch_id"]
        publishes = []

        def publish_fn(**kwargs):
            publishes.append(kwargs["article"]["event_id"])
            return {"url": "https://coinnetwork.example/p/1", "post_id": 11}

        first = handle_callback(
            callback_data("ap", batch_id, "event-001"),
            store=self.store,
            publish_fn=publish_fn,
        )
        second = handle_callback(
            callback_data("ap", batch_id, "event-001"),
            store=self.store,
            publish_fn=publish_fn,
        )
        self.assertTrue(first["published"])
        self.assertFalse(first.get("duplicate"))
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(publishes, ["event-001"])
        self.assertEqual(self.store.read_story(batch_id, "event-001")["state"], STATE_PUBLISHED)

        rejected = handle_callback(
            callback_data("rj", batch_id, "event-002"),
            store=self.store,
            publish_fn=publish_fn,
        )
        self.assertEqual(rejected["state"], STATE_REJECTED)
        self.assertFalse(rejected["published"])
        after_pub_reject = handle_callback(
            callback_data("rj", batch_id, "event-001"),
            store=self.store,
            publish_fn=publish_fn,
        )
        self.assertEqual(after_pub_reject["action"], "reject_ignored")
        self.assertEqual(self.store.read_story(batch_id, "event-001")["state"], STATE_PUBLISHED)

        def boom(**kwargs):
            raise WordPressPublishError("post_failed", f"auth {TOKEN} failed")

        failed = handle_callback(
            callback_data("ap", batch_id, "event-004"),
            store=self.store,
            publish_fn=boom,
        )
        self.assertFalse(failed["published"])
        self.assertEqual(failed["state"], "PUBLISH_FAILED")
        self.assertNotIn(TOKEN, failed.get("reason") or "")

    def test_approve_wordpress_disabled_records_without_publish(self) -> None:
        gen = handle_update(
            self._make_update(user_id=55),
            config=self.config,
            client=self.client,
            store=self.store,
            pipeline=self._pipeline(),
        )
        batch_id = gen["batch_id"]
        calls: list[dict] = []

        def hold(**kwargs):
            calls.append(kwargs)
            return wordpress_disabled_publish(**kwargs)

        first = handle_callback(
            callback_data("ap", batch_id, "event-001"),
            store=self.store,
            publish_fn=hold,
        )
        second = handle_callback(
            callback_data("ap", batch_id, "event-001"),
            store=self.store,
            publish_fn=hold,
        )
        ignored = handle_callback(
            callback_data("rj", batch_id, "event-001"),
            store=self.store,
            publish_fn=hold,
        )
        story = self.store.read_story(batch_id, "event-001")
        self.assertTrue(first["ok"])
        self.assertFalse(first["published"])
        self.assertEqual(first["state"], STATE_APPROVED)
        self.assertTrue(first.get("wordpress_disabled"))
        self.assertIsNone(first.get("url"))
        self.assertIn("disabled", (first.get("telegram_caption_suffix") or "").lower())
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(ignored["action"], "reject_ignored")
        self.assertEqual(len(calls), 1)
        self.assertEqual(story["state"], STATE_APPROVED)
        self.assertTrue(story.get("wordpress_disabled"))
        self.assertIsNone(story.get("wp_url"))

    def test_listen_loop_polls_once_offline(self) -> None:
        class PollTransport(RecordingTransport):
            def __call__(self, *args, **kwargs):
                method = kwargs.get("method")
                if method == "getUpdates":
                    self.calls.append({"args": args, "kwargs": kwargs, "method": method})
                    return FakeResponse(200, {"ok": True, "result": []})
                return super().__call__(*args, **kwargs)

        transport = PollTransport()
        client = TelegramTestClient(self.config, transport=transport)
        run_listen_loop(
            config=self.config,
            client=client,
            store=self.store,
            pipeline=lambda: {"ok": True},
            wp_publish_fn=wordpress_disabled_publish,
            poll_timeout=0,
            max_iterations=1,
        )
        self.assertEqual(transport.calls[0]["method"], "getUpdates")
        self.assertEqual(transport.calls[0]["kwargs"]["json"]["timeout"], 0)

    def test_wordpress_adapter_sanitizes_and_uses_frozen_fields(self) -> None:
        calls = []

        def transport(method, url, **kwargs):
            calls.append({"method": method, "url": url, "json": kwargs.get("json")})
            if "media" in url:
                return {"ok": True, "payload": {"id": 9}}
            return {"ok": True, "payload": {"id": 44, "link": "https://coinnetwork.example/p/44"}}

        cfg = WordPressConfig(
            base_url="https://coinnetwork.example",
            username="editor",
            app_password="secret-app-pass-value",
        )
        result = publish_frozen_story(
            config=cfg,
            article={
                "headline": "Frozen headline",
                "slug": "frozen-headline",
                "article_body": "Frozen body",
                "dek": "Frozen dek",
                "meta_description": "Frozen meta",
            },
            image_path=str(PIXEL),
            transport=transport,
        )
        self.assertEqual(result["url"], "https://coinnetwork.example/p/44")
        self.assertEqual(calls[1]["json"]["title"], "Frozen headline")
        self.assertEqual(calls[1]["json"]["slug"], "frozen-headline")
        self.assertNotIn("secret-app-pass-value", sanitize_wp_error("boom secret-app-pass-value", cfg.secrets()))
        with self.assertRaises(WordPressConfigError):
            load_wordpress_config(None)

    def test_image_concurrency_bounded(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()

        def image_fn(job):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"success": True, "event_id": job["event_id"], "final_path": str(PIXEL)}

        jobs = [{"event_id": event_id} for event_id in IDS]
        run_image_jobs(jobs, image_fn, parallel=True, max_workers=IMAGE_MAX_CONCURRENCY)
        self.assertLessEqual(peak, IMAGE_MAX_CONCURRENCY)
        self.assertEqual(IMAGE_MAX_CONCURRENCY, 3)

    def test_cli_refuses_live_listen(self) -> None:
        self.assertEqual(control_main([]), 2)
        self.assertIn("--listen --live", LISTEN_COMMAND)
        self.assertFalse(LIVE_SEND_ENABLED)


if __name__ == "__main__":
    unittest.main()


