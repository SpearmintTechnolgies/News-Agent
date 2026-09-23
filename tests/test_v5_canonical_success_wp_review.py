"""Mocked regression for the canonical V5 generation-success path."""

from __future__ import annotations

import sys
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.publication.master_index import MasterIndexStore
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
from newsagent_v2.v5_generation.generation_worker import GenerationWorker
from newsagent_v2.v5_generation.persistent_review import PersistentReviewStore
from newsagent_v2.v5_generation.persistent_store import GenerationJob, PersistentV5Store
from newsagent_v2.v5_generation.version_store import VersionStore
from newsagent_v2.wordpress.draft_lifecycle import DraftResult
from newsagent_v2.wordpress.seo_metadata import SEOValidationResult, SEOMetadata, build_seo_for_article


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "message_id": len(self.messages)}

    def edit_message_text(self, **kwargs):
        return {"ok": True, "message_id": kwargs.get("message_id")}

    def send_photo(self, **kwargs):
        return {"ok": True, "message_id": len(self.messages) + 1}


class FakeLifecycle:
    def __init__(self) -> None:
        self.create_calls = 0
        self.publish_calls = 0
        self.post_id = 91
        self.create_kwargs = None

    def create_or_update_draft(self, **kwargs):
        self.create_calls += 1
        self.create_kwargs = kwargs
        return DraftResult(
            ok=True, event_id=kwargs["event_id"], wp_post_id=self.post_id,
            wp_url="https://cms.test/wp/story", status="draft", created=self.create_calls == 1,
            updated=self.create_calls > 1, category_ids=[7], tag_ids=[8],
            seo=SEOMetadata("Canonical Story", "Canonical Story", "A verified story with enough detail for metadata", "canonical story", "canonical-story", None, None, None, None, None),
            seo_validation=SEOValidationResult("PASS", 100, [], []),
        )

    def publish_draft(self, event_id):
        self.publish_calls += 1
        return DraftResult(
            ok=True, event_id=event_id, wp_post_id=self.post_id,
            wp_url="https://cms.test/story", status="publish",
        )


def test_canonical_success_creates_one_draft_enriched_review_and_publishes_same_post(tmp_path, monkeypatch):
    event_id = "evt-canonical-success"
    state = PersistentV5Store(tmp_path / "state")
    versions = VersionStore(tmp_path / "versions")
    article = {
        "headline": "Canonical Story", "dek": "A verified story", "article_body": "Body",
        "category": "Markets", "tags": ["rates"],
    }
    versions.save_article(event_id, "v1", article, "article-hash", {"qa_passed": True}, {})
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    versions.save_image(event_id, "v1", str(image), "image-hash", {})

    job = GenerationJob(
        job_id="job-canonical", event_id=event_id, discovery_run_id="run:evt-canonical-success",
        state="REQUESTING", created_at="now", updated_at="now",
    )
    state.save_job(job)
    client = FakeClient()
    config = TelegramConfig(bot_token="token", test_chat_id="chat")
    lifecycle = FakeLifecycle()
    worker = GenerationWorker(
        client, config, state,
        environ={"V5_VERSION_STORE_ROOT": str(tmp_path / "versions")},
        wordpress_lifecycle=lifecycle,
    )
    event = NewsEvent(
        event_id=event_id, canonical_title="Canonical Story", topic="markets",
        entities=frozenset({"interest-rates", "central-bank"}),
    )

    with patch("newsagent_v2.v5_generation.generation_worker.RunStoryAdapter") as adapter_cls:
        adapter_cls.return_value.run_story.return_value = {
            "ok": True, "article_version": "v1", "image_version": "v1",
            "article_hash": "article-hash", "image_hash": "image-hash",
            "text_usage": {"provider": "kimi", "model": "kimi-k2.5", "requests": 2, "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "cost_usd": 0.0123},
            "kimi_usage": {"provider": "kimi", "model": "kimi-k2.5", "requests": 2, "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500, "cost_usd": 0.0123},
            "image_usage": {"provider": "gemini", "model": "gemini-2.5-flash-image", "requests": 1, "provider_reported_usage": {"promptTokenCount": 40, "totalTokenCount": 840}, "provider_reported_cost_usd": 0.0045},
        }
        result = worker.run_generation(event, job)

    assert result["ok"] is True
    assert lifecycle.create_calls == 1
    review_text = "\n".join(message["text"] for message in client.messages)
    assert "WP Draft: #91" in review_text
    assert "Body" not in review_text
    assert "SEO: PASS" in review_text
    assert "Categories: Markets" in review_text and "Tags: rates, central-bank, interest-rates" in review_text
    assert "TEXT — kimi/kimi-k2.5" in review_text
    assert "Input: 1000" in review_text and "Output: 500" in review_text
    assert "IMAGE — Gemini Flash/gemini-2.5-flash-image" in review_text
    assert "Cost: $0.0123 USD" in review_text and "Cost: $0.0045 USD" in review_text
    assert "TOTAL: $0.0168 USD" in review_text
    assert sum(1 for message in client.messages if message.get("reply_markup")) == 1
    assert lifecycle.create_kwargs["categories"] == ["Markets"]
    assert lifecycle.create_kwargs["tags"] == ["rates", "central-bank", "interest-rates"]

    _, repaired_validation = build_seo_for_article(
        {"headline": "Central Bank Signals New Interest Rate Policy", "dek": "Officials outlined a measured change in interest rate policy for the coming quarter.", "article_body": "Central bank officials outlined a measured change in interest rate policy for the coming quarter. The statement described the decision and its expected effect on markets and rates without adding unsupported claims."},
        "https://cms.test", repair=True,
    )
    assert repaired_validation.status == "PASS"

    review = PersistentReviewStore(tmp_path / "review")
    index = MasterIndexStore(tmp_path / "index")
    import newsagent_v2.telegram.v5_review_callbacks as callbacks_module
    monkeypatch.setattr(callbacks_module, "DATA_ROOT", tmp_path / "publications")
    (tmp_path / "publications").mkdir()
    handler = V5ReviewCallbackHandler(
        review_store=review, revision_controller=None, version_store=versions,
        persistent_store=state, wordpress_lifecycle=lifecycle, master_index=index,
        environ={
            "NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://cms.test",
            "NEWSAGENT_V2_WORDPRESS_USERNAME": "user",
            "NEWSAGENT_V2_WORDPRESS_APP_PASSWORD": "pass",
        },
    )
    approved = handler.handle(f"approve:{event_id}:v1")
    assert approved["ok"] is True
    first = handler.handle(f"publish:{event_id}:v1:v1")
    second = handler.handle(f"publish:{event_id}:v1:v1")

    assert first["url"] == "https://cms.test/story"
    assert second["url"] == first["url"]
    assert lifecycle.publish_calls == 1
    assert len(index.list_records()) == 1
    assert index.load(event_id).wp_post_id == 91


def test_review_rehydrates_persisted_usage_prices_and_admin_draft_url(tmp_path):
    event_id = "evt-persisted-telemetry"
    versions = VersionStore(tmp_path / "versions")
    versions.save_article(event_id, "v1", {"headline": "Persisted", "dek": "Dek", "article_body": "Body"}, "hash", {}, {})
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    versions.save_image(event_id, "v1", str(image), "image-hash", {})

    stories = tmp_path / "stories"
    budget_dir = stories / f"STORY-{event_id}" / "kimibudget"
    budget_dir.mkdir(parents=True)
    (budget_dir / "budget.json").write_text(json.dumps({
        "event_id": event_id, "request_count": 1, "prompt_tokens": 4229,
        "completion_tokens": 616, "total_tokens": 4845, "status": "NORMAL",
    }), encoding="utf-8")
    make_runs = tmp_path / "make_runs" / f"{event_id}-run-vertex"
    make_runs.mkdir(parents=True)
    (make_runs / "provider_result.json").write_text(json.dumps({
        "provider_name": "vertex", "model_name": "gemini-3.1-flash-image",
        "provider_reported_usage": {
            "promptTokenCount": 211, "candidatesTokenCount": 1120,
            "totalTokenCount": 1331,
            "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
        }, "provider_reported_cost_usd": None, "estimated_list_price_usd": None,
    }), encoding="utf-8")

    from newsagent_v2.v5_generation.telegram_delivery import send_initial_v5_review_package
    client = FakeClient()
    environ = {
        "V5_STORIES_ROOT": str(stories), "V5_MAKE_RUNS_ROOT": str(tmp_path / "make_runs"),
        "NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://coinnetwork.info",
        "NEWSAGENT_V2_KIMI_PRICING_VERIFIED": "true",
        "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK": "1",
        "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK": "2",
        "NEWSAGENT_V2_VERTEX_PRICING_VERIFIED": "true",
        "NEWSAGENT_V2_VERTEX_INPUT_USD_PER_MTOK": "1",
        "NEWSAGENT_V2_VERTEX_IMAGE_OUTPUT_USD_PER_MTOK": "10",
    }
    result = send_initial_v5_review_package(
        client, TelegramConfig("token", "chat"), versions, event_id, "Persisted", "v1", "v1",
        {"wordpress_draft": {"wp_post_id": 14327, "wp_url": "https://coinnetwork.info/?p=14327", "status": "draft"}},
        environ=environ,
    )
    assert result["ok"] is True
    card = "\n".join(message["text"] for message in client.messages)
    assert "Requests: 1 | Input: 4229 | Output: 616 | Total: 4845" in card
    assert "IMAGE — vertex/gemini-3.1-flash-image" in card
    assert "Input: 211 | Output/Image: 1120 | Total: 1331" in card
    assert "TOTAL COST: $0.0169 USD" in card
    url_buttons = [button for row in client.messages[-1]["reply_markup"]["inline_keyboard"] for button in row if button.get("text") == "OPEN DRAFT"]
    assert url_buttons == [{"text": "OPEN DRAFT", "url": "https://coinnetwork.info/wp-admin/post.php?post=14327&action=edit"}]

    missing_price_client = FakeClient()
    send_initial_v5_review_package(
        missing_price_client, TelegramConfig("token", "chat"), versions, event_id, "Persisted", "v1", "v1",
        {"wordpress_draft": {"wp_post_id": 14327, "wp_url": "https://coinnetwork.info/?p=14327", "status": "draft"}},
        environ={"V5_STORIES_ROOT": str(stories), "V5_MAKE_RUNS_ROOT": str(tmp_path / "make_runs"), "NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://coinnetwork.info"},
    )
    missing_card = "\n".join(message["text"] for message in missing_price_client.messages)
    assert "Cost: unavailable USD" in missing_card
    assert "TOTAL COST: unavailable" in missing_card


def test_edit_creates_version_updates_same_draft_and_publishes_once(tmp_path, monkeypatch):
    event_id = "evt-edit"
    state = PersistentV5Store(tmp_path / "state")
    versions = VersionStore(tmp_path / "versions")
    article = {
        "headline": "Markets Report Rates", "dek": "Markets report rates in the current update.",
        "article_body": "Markets report rates in the current update.", "category": "Markets", "tags": ["rates"],
    }
    article_input = {"evidence": [{"url": "https://source.test", "title": "Markets report rates"}]}
    versions.save_article(event_id, "v1", article, "hash-v1", {"qa_publishable": True}, {"article_input": article_input, "text_usage": {"provider": "kimi", "model": "k2", "requests": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_usd": 0.001}, "image_usage": {"model": "gemini-flash", "requests": 1, "provider_reported_cost_usd": 0.002}})
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    versions.save_image(event_id, "v1", str(image), "image-hash", {})
    lifecycle = FakeLifecycle()
    review = PersistentReviewStore(tmp_path / "review")
    index = MasterIndexStore(tmp_path / "index")
    import newsagent_v2.telegram.v5_review_callbacks as callbacks_module
    monkeypatch.setattr(callbacks_module, "DATA_ROOT", tmp_path / "publications")
    (tmp_path / "publications").mkdir()
    handler = V5ReviewCallbackHandler(
        review_store=review, revision_controller=__import__("newsagent_v2.v5_generation.revision_controller", fromlist=["RevisionController"]).RevisionController(versions, {}),
        version_store=versions, persistent_store=state, wordpress_lifecycle=lifecycle, master_index=index,
        environ={"NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://cms.test", "NEWSAGENT_V2_WORDPRESS_USERNAME": "u", "NEWSAGENT_V2_WORDPRESS_APP_PASSWORD": "p"},
    )
    start = handler.handle(f"edit:{event_id}:v1", chat_id="chat")
    assert start["action"] == "edit_mode"
    with patch("newsagent_v2.article.qa.runner.run_article_qa", return_value={"qa_publishable": True}) as qa_mock:
        edited = handler.check_and_capture_feedback_text("chat", "Replace Markets with Rates")
    assert edited["ok"] is True
    assert edited["article_version"] == "v2"
    assert qa_mock.called
    assert versions.get_article(event_id, "v2")["article"]["article_body"].startswith("Rates report")
    assert lifecycle.create_calls == 1
    assert lifecycle.create_kwargs["article_version"] == "v2"
    assert lifecycle.post_id == 91

    client = FakeClient()
    from newsagent_v2.v5_generation.telegram_delivery import send_initial_v5_review_package
    send_initial_v5_review_package(client, TelegramConfig("token", "chat"), versions, event_id, "Rates report", "v2", "v1", edited)
    card_text = "\n".join(message["text"] for message in client.messages)
    assert "READY FOR REVIEW" in card_text and "Markets Report Rates" in card_text
    assert "article_body" not in card_text
    assert any(button["text"] == "EDIT" for row in client.messages[-1]["reply_markup"]["inline_keyboard"] for button in row)

    assert handler.handle(f"approve:{event_id}:v2")["ok"] is True
    published = handler.handle(f"publish:{event_id}:v2:v1")
    assert published["post_id"] == 91
    assert lifecycle.publish_calls == 1
    assert len(index.list_records()) == 1