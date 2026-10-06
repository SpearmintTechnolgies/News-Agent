"""Bylines come from the site's users and are written onto the WordPress post."""

from __future__ import annotations

from pathlib import Path

from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
from newsagent_v2.v5_generation.telegram_delivery import paint_author_flow, review_keyboard
from newsagent_v2.wordpress.authors import (
    SiteAuthor,
    author_for,
    author_id_for,
    label_for,
    list_site_authors,
    remember_default,
    remember_event,
)
from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle
from newsagent_v2.wordpress.draft_store import WordPressDraftStore


def _config() -> WordPressConfig:
    return WordPressConfig("https://site.test", "editor", "app-secret")


def test_list_keeps_bylines_and_drops_subscribers():
    def transport(method, url, **kwargs):
        assert method == "GET" and "who=authors" in url
        return {"ok": True, "payload": [
            {"id": 4, "name": "Ada Stone", "roles": ["author"]},
            {"id": 9, "name": "Reader", "roles": ["subscriber"]},
            {"id": "nope", "name": "Broken"},
        ]}

    authors = list_site_authors(_config(), transport)
    assert [author.name for author in authors] == ["Ada Stone"]


def test_public_users_fill_in_when_the_author_query_is_empty():
    calls: list[str] = []

    def transport(method, url, **kwargs):
        calls.append(url)
        if "who=authors" in url:
            return {"ok": True, "payload": []}
        return {"ok": True, "payload": [
            {"id": 19, "name": "Desk Editor", "slug": "desk"},
            {"id": 8, "name": "Reader", "roles": ["subscriber"]},
        ]}

    authors = list_site_authors(_config(), transport)
    assert [author.name for author in authors] == ["Desk Editor"]
    assert any("who=authors" in url for url in calls)
    assert any("who=authors" not in url and url.endswith("per_page=100") for url in calls)


def test_story_choice_overrides_the_default(tmp_path: Path):
    path = tmp_path / "author.json"
    remember_default(SiteAuthor(4, "Ada Stone"), path)
    remember_event("evt-1", SiteAuthor(7, "Sam Cole"), path)
    assert author_for("evt-1", path).name == "Sam Cole"
    assert author_id_for("evt-2", path) == 4
    assert label_for("evt-missing", path) == "Ada Stone"
    assert label_for("evt-none", tmp_path / "missing.json") == "not chosen"


def test_draft_and_publish_use_the_chosen_author(tmp_path: Path, monkeypatch):
    path = tmp_path / "author.json"
    remember_default(SiteAuthor(4, "Ada Stone"), path)
    monkeypatch.setattr("newsagent_v2.wordpress.draft_lifecycle.author_id_for", lambda event_id: author_id_for(event_id, path))
    calls: list[dict] = []

    def transport(method, url, **kwargs):
        body = kwargs.get("json") or {}
        calls.append({"method": method, "url": url, "body": body})
        if method == "POST" and url.endswith("/posts"):
            return {"ok": True, "payload": {"id": 15, "link": "https://site.test/ada", "modified": "t"}}
        if "rankmath" in url:
            return {"ok": True, "payload": {"success": True}}
        return {"ok": True, "payload": {"id": 15, "link": "https://site.test/ada", "modified": "t2"}}

    store = WordPressDraftStore(root=tmp_path / "drafts")
    lifecycle = WordPressDraftLifecycle(_config(), transport, store=store)
    created = lifecycle.create_or_update_draft("evt-1", {"headline": "Ada", "article_body": "Body", "slug": "ada"}, "v1", format_html=False)
    assert created.ok
    assert calls[0]["body"]["author"] == 4
    published = lifecycle.publish_draft("evt-1")
    assert published.ok
    assert calls[-1]["body"] == {"status": "publish", "author": 4}


def test_review_card_can_switch_one_story(tmp_path: Path, monkeypatch):
    path = tmp_path / "author.json"
    monkeypatch.setattr("newsagent_v2.wordpress.authors.DEFAULT_PATH", path)

    class Lifecycle:
        config = _config()

        def __init__(self):
            self.assigned: tuple[str, int] | None = None

        def transport(self, method, url, **kwargs):
            return {"ok": True, "payload": [{"id": 7, "name": "Sam Cole", "roles": ["editor"]}]}

        def assign_author(self, event_id, user_id):
            self.assigned = (event_id, user_id)
            from newsagent_v2.wordpress.draft_lifecycle import DraftResult
            return DraftResult(ok=True, event_id=event_id, wp_post_id=3)

    lifecycle = Lifecycle()
    handler = V5ReviewCallbackHandler(None, None, wordpress_lifecycle=lifecycle)
    menu = handler.handle("author:evt-1:v1:v1")
    assert menu["edit_card"] is True
    assert menu["authors"][0].name == "Sam Cole"
    saved = handler.handle("set_author:evt-1:7:v1:v1")
    assert saved["edit_card"] is True
    assert saved["author_name"] == "Sam Cole"
    assert saved["selected_id"] == 7
    assert lifecycle.assigned == ("evt-1", 7)
    assert author_for("evt-1", path).name == "Sam Cole"


def test_author_buttons_and_flow_stay_on_the_card():
    keyboard = review_keyboard(
        "evt-1", "v2", "v1",
        authors=[SiteAuthor(7, "Sam Cole"), SiteAuthor(4, "Ada Stone")],
        selected_id=7,
    )
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]
    sam = next(button for button in buttons if "Sam" in button["text"])
    assert sam["text"].startswith("✓ ")
    assert sam["callback_data"] == "set_author:evt-1:7:v2:v1"
    assert any(button["text"] == "APPROVE & PUBLISH" for button in buttons)
    painted = paint_author_flow("READY\nAuthor: not chosen\nTags: none", "Sam Cole")
    assert "2. Author: Sam Cole" in painted
    assert "Author: Sam Cole" in painted
