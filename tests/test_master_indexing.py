from __future__ import annotations

from pathlib import Path

from newsagent_v2.approval.callbacks import handle_callback
from newsagent_v2.approval.store import ApprovalStore, STATE_AWAITING_APPROVAL
from newsagent_v2.publication.master_index import MasterIndexStore
from newsagent_v2.telegram.cards import callback_data
from newsagent_v2.wordpress.adapter import WordPressPublishError


def _story() -> dict:
    return {
        "event_id": "event-001",
        "state": STATE_AWAITING_APPROVAL,
        "article": {"event_id": "event-001", "article_body": "frozen body"},
        "article_version": "v3",
        "image_version": "v2",
        "categories": ["market_move"],
        "tags": ["bitcoin"],
        "seo_status": "PASS",
    }


def test_master_index_is_written_after_successful_publish(tmp_path: Path) -> None:
    approval = ApprovalStore(tmp_path / "approval")
    index = MasterIndexStore(tmp_path / "master-index")
    approval.write_story("batch-1", "event-001", _story())

    result = handle_callback(
        callback_data("ap", "batch-1", "event-001"),
        store=approval,
        index_store=index,
        publish_fn=lambda **_: {"url": "https://news.example/story", "post_id": 42},
    )

    assert result["published"] is True
    assert result["indexing_state"] == "RECORDED"
    record = index.load("event-001")
    assert record is not None
    assert record.to_dict() == {
        "event_id": "event-001",
        "canonical_url": "https://news.example/story",
        "wp_post_id": 42,
        "article_version": "v3",
        "image_version": "v2",
        "categories": ["market_move"],
        "tags": ["bitcoin"],
        "seo_status": "PASS",
        "published_at": record.published_at,
        "indexing_state": "RECORDED",
    }


def test_master_index_is_not_written_when_publish_fails(tmp_path: Path) -> None:
    approval = ApprovalStore(tmp_path / "approval")
    index = MasterIndexStore(tmp_path / "master-index")
    approval.write_story("batch-1", "event-001", _story())

    def fail(**_: object) -> dict:
        raise WordPressPublishError("post_failed", "mock failure")

    result = handle_callback(
        callback_data("ap", "batch-1", "event-001"),
        store=approval,
        index_store=index,
        publish_fn=fail,
    )

    assert result["published"] is False
    assert index.load("event-001") is None