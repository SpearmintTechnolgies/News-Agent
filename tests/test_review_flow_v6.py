"""V6 review flow: review card, REVISE / EDIT in the background, stale-card protection."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
from newsagent_v2.v5_generation.generation_worker import GenerationWorker
from newsagent_v2.v5_generation.persistent_review import FeedbackRecord, PersistentReviewStore
from newsagent_v2.v5_generation.run_story_adapter import Revision
from newsagent_v2.v5_generation.telegram_delivery import send_initial_v5_review_package
from newsagent_v2.v5_generation.version_store import VersionStore

EVENT = "evt-flow"
BODY = (
    "Bitcoin ETF inflows rose to $1.2 billion on Monday. Analysts at the fund tracked the move.\n\n"
    "Spot Bitcoin ETF demand has grown for three weeks. Issuers reported steady creations."
)


def _article(headline: str = "Bitcoin ETF inflows rise to $1.2 billion") -> dict:
    return {"headline": headline, "dek": "Spot funds extend their run.", "article_body": BODY}


@pytest.fixture
def stores(tmp_path):
    versions = VersionStore(tmp_path / "versions")
    versions.save_article(EVENT, "v1", _article(), "hash-v1", {}, {})
    reviews = PersistentReviewStore(tmp_path / "reviews")
    return versions, reviews


def _handler(versions, reviews, worker=None):
    return V5ReviewCallbackHandler(
        review_store=reviews, version_store=versions, generation_worker=worker, environ={},
    )


def _feedback(reviews, artifact: str, version: str, text: str) -> None:
    reviews.save_feedback(FeedbackRecord(
        feedback_id=f"fb-{artifact}-{version}", event_id=EVENT, job_id="job-1",
        artifact_type=artifact, version=version, feedback_text=text, reviewer="editor",
    ))


class _Client:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "message_id": len(self.messages)}


def test_review_card_labels_revisions_and_carries_versioned_buttons(stores):
    versions, _ = stores
    versions.save_article(EVENT, "v2", _article(), "hash-v2", {}, {})
    client = _Client()
    result = send_initial_v5_review_package(
        client, SimpleNamespace(test_chat_id="chat"), versions, EVENT, "Title", "v2", None,
        generation_result={"wordpress_draft": {"wp_post_id": 7, "status": "draft"}},
        environ={"NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://site.test"},
    )
    assert result["ok"] is True
    card = client.messages[-1]
    assert "revised (v2)" in card["text"]
    callbacks = [b.get("callback_data") for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert f"approve:{EVENT}:v2" in callbacks
    assert f"edit:{EVENT}:v2" in callbacks
    assert card["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("post=7&action=edit")


def test_revise_requires_feedback_for_current_version(stores):
    versions, reviews = stores
    worker = MagicMock()
    _feedback(reviews, "article", "v0", "old feedback for another version")
    result = _handler(versions, reviews, worker).handle(f"revise:{EVENT}:v1:")
    assert result["ok"] is False
    assert result["reason"] == "no_feedback"
    worker.start_revision.assert_not_called()


def test_revise_starts_background_rewrite_with_latest_feedback(stores):
    versions, reviews = stores
    worker = MagicMock()
    worker.start_revision.return_value = {"ok": True, "job_id": "job-2"}
    _feedback(reviews, "article", "v1", "Lead with the inflow figure.")
    result = _handler(versions, reviews, worker).handle(f"revise:{EVENT}:v1:")
    assert result["ok"] is True
    assert result["action"] == "revise"
    assert "new review card" in result["message"]
    event_id, revision = worker.start_revision.call_args.args
    assert event_id == EVENT
    assert revision == Revision(
        article_feedback="Lead with the inflow figure.", image_feedback="",
        article_version="v1", image_version=None,
    )
    assert worker.start_revision.call_args.kwargs["headline"] == _article()["headline"]


def test_revise_enforces_revision_limit(stores):
    versions, reviews = stores
    for n in range(2, 6):
        versions.save_article(EVENT, f"v{n}", _article(), f"hash-v{n}", {}, {})
    _feedback(reviews, "article", "v5", "Again.")
    handler = _handler(versions, reviews, MagicMock())
    handler.max_article_revisions = 3
    result = handler.handle(f"revise:{EVENT}:v5:")
    assert result["reason"] == "revision_limit"


def test_edit_saves_new_version_and_refreshes_draft(stores):
    versions, reviews = stores
    worker = MagicMock()
    result = _handler(versions, reviews, worker).handle_edit_instruction(
        EVENT, "v1", "replace steady creations with steady fund creations",
    )
    assert result["ok"] is True
    assert result["article_version"] == "v2"
    saved = versions.get_article(EVENT, "v2")
    assert "steady fund creations" in saved["article"]["article_body"]
    assert saved["metadata"]["edited_from"] == "v1"
    worker.apply_edit.assert_called_once_with(EVENT, "v2", None)


def test_edit_refuses_ungrounded_text(stores):
    versions, reviews = stores
    worker = MagicMock()
    result = _handler(versions, reviews, worker).handle_edit_instruction(
        EVENT, "v1", "replace Monday with Tuesday",
    )
    assert result["reason"] == "unsupported_edit"
    worker.apply_edit.assert_not_called()


def test_edit_refuses_stale_version(stores):
    versions, reviews = stores
    versions.save_article(EVENT, "v2", _article(), "hash-v2", {}, {})
    result = _handler(versions, reviews, MagicMock()).handle_edit_instruction(
        EVENT, "v1", "replace steady creations with steady fund creations",
    )
    assert result["reason"] == "stale_version"


def test_approve_from_outdated_card_is_refused(stores):
    versions, reviews = stores
    versions.save_article(EVENT, "v2", _article(), "hash-v2", {}, {})
    handler = _handler(versions, reviews)
    handler.handle_publish = MagicMock()
    result = handler.handle(f"approve:{EVENT}:v1")
    assert result["ok"] is False
    assert result["reason"] == "stale_version"
    assert reviews.has_approval(EVENT) is False
    handler.handle_publish.assert_not_called()


def test_worker_runs_revision_in_background(monkeypatch):
    worker = GenerationWorker.__new__(GenerationWorker)
    done = threading.Event()
    seen = {}

    def fake_run(event, job, revision=None):
        seen.update(event=event, job=job, revision=revision)
        done.set()
        return {"ok": True}

    monkeypatch.setattr(worker, "_running_job", lambda event_id: None)
    monkeypatch.setattr(worker, "_load_event", lambda event_id, headline="": SimpleNamespace(event_id=event_id))
    monkeypatch.setattr(worker, "_new_job", lambda event_id: SimpleNamespace(job_id="job-9", event_id=event_id))
    monkeypatch.setattr(worker, "run_generation", fake_run)
    revision = Revision(article_feedback="Shorter intro.", image_feedback="", article_version="v1", image_version="v1")

    started = worker.start_revision(EVENT, revision, headline="Headline")

    assert started == {"ok": True, "job_id": "job-9", "new": True}
    assert done.wait(5)
    assert seen["revision"] is revision
    assert seen["event"].event_id == EVENT


def test_worker_refuses_revision_while_story_is_running(monkeypatch):
    worker = GenerationWorker.__new__(GenerationWorker)
    monkeypatch.setattr(worker, "_running_job", lambda event_id: SimpleNamespace(job_id="job-1"))
    result = worker.start_revision(EVENT, Revision("x", "", "v1", None))
    assert result["reason"] == "busy"
