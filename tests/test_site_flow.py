"""A website opens its own categories, then its own authors."""

from __future__ import annotations

from newsagent_v2.control.site_flow import (
    author_keyboard,
    category_keyboard,
    category_labels,
    category_menu_text,
)
from newsagent_v2.control.story_picker import category_queries, skips_age_cap, story_matches
from newsagent_v2.wordpress.authors import SiteAuthor


def _event(title: str):
    class Report:
        published_at = "2026-10-05T12:00:00+00:00"

        def __init__(self, headline: str) -> None:
            self.title = headline
            self.topics = []

    class Event:
        canonical_title = title
        topic = ""
        reports = [Report(title)]

    return Event()


def test_each_site_shows_its_own_categories_and_then_its_authors():
    categories = [
        {"id": 52, "name": "Bitcoin News", "parent": 17},
        {"id": 35, "name": "Business", "parent": 0},
        {"id": 178, "name": "Business", "parent": 17},
        {"id": 17, "name": "Latest News", "parent": 0},
    ]
    labels = [row["label"] for row in category_labels(categories)]
    assert "Latest News · Business" in labels
    assert "Business" in labels
    keyboard = category_keyboard("abcd1234", categories)
    callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
    assert "c:abcd1234:52" in callbacks
    assert all(len(item) <= 64 for item in callbacks)
    text = category_menu_text("Coinography", 4)
    assert "Coinography" in text and "4 categories" in text

    authors = author_keyboard("abcd1234", [SiteAuthor(19, "DMI@Coinography.com"), SiteAuthor(1, "Ahmed Falah")])
    author_callbacks = [button["callback_data"] for row in authors["inline_keyboard"] for button in row]
    assert author_callbacks == ["a:abcd1234:19", "a:abcd1234:1"]


def test_a_site_category_filters_stories_by_its_own_name():
    assert skips_age_cap("Bitcoin News")
    assert not skips_age_cap("trend")
    assert story_matches(_event("Bitcoin miners sell into the rally"), "Bitcoin News")
    assert not story_matches(_event("Ethereum fees drop after the upgrade"), "Bitcoin News")
    assert category_queries("Bitcoin News") == ("bitcoin",)
    assert category_queries("bitcoin") == ("bitcoin",)


def test_tapped_category_id_is_the_one_that_is_filed(tmp_path, monkeypatch):
    from newsagent_v2.control import site_flow
    from newsagent_v2.control.sites import COIN_NETWORK_SITE_ID
    from newsagent_v2.publication.master_index import MasterIndexStore
    from newsagent_v2.story6 import _category_on_active_site
    from newsagent_v2.wordpress import authors
    from newsagent_v2.wordpress.config import WordPressConfig
    from newsagent_v2.wordpress.draft_lifecycle import WordPressDraftLifecycle
    from newsagent_v2.wordpress.draft_store import WordPressDraftStore

    monkeypatch.setattr(site_flow, "FLOW_PATH", tmp_path / "flow.json")
    authors.active_site_id = "318215a2"
    try:
        empty_name, empty_id = _category_on_active_site("Policy")
        assert empty_name == "" and empty_id == 0
        site_flow.remember_publication("318215a2", 178, "Business", SiteAuthor(1, "Ada"))
        name, category_id = _category_on_active_site("Policy")
        assert name == "Business" and category_id == 178
    finally:
        authors.active_site_id = ""

    authors.active_site_id = COIN_NETWORK_SITE_ID
    try:
        fallback, fallback_id = _category_on_active_site("Policy")
        assert fallback == "Policy & Regulations" and fallback_id == 0
    finally:
        authors.active_site_id = ""

    posted: list[dict] = []

    def transport(method, url, **kwargs):
        if method == "POST" and url.rstrip("/").endswith("/posts"):
            posted.append(kwargs.get("json") or {})
            return {"ok": True, "payload": {"id": 9, "link": "https://news.example/a", "modified": "t"}}
        if method == "POST" and url.rstrip("/").endswith("/tags"):
            return {"ok": True, "payload": {"id": 4, "name": "Markets"}}
        if "rankmath" in url:
            return {"ok": True, "payload": {"success": True}}
        return {"ok": True, "payload": []}

    lifecycle = WordPressDraftLifecycle(
        WordPressConfig("https://news.example.com", "editor", "app-secret"),
        transport,
        store=WordPressDraftStore(root=tmp_path / "drafts"),
        master_index=MasterIndexStore(root=tmp_path / "index"),
    )
    result = lifecycle.create_or_update_draft(
        "evt-cat",
        {
            "headline": "Two Business categories",
            "article_body": "The business desk filed this story in one category.",
            "slug": "two-business",
            "dek": "A short dek for the business desk story today.",
            "meta_description": "A short dek for the business desk story today.",
            "wp_category_id": 178,
            "categories": ["Business"],
        },
        "v1",
        categories=["Business"],
        tags=["Markets"],
        format_html=False,
    )
    assert result.ok, result.error
    assert posted[0]["categories"] == [178]
    refused = lifecycle.create_or_update_draft(
        "evt-missing",
        {"headline": "No category", "article_body": "Body.", "category_choice_missing": True},
        "v1",
        categories=["Policy & Regulations"],
        format_html=False,
    )
    assert refused.ok is False
    assert refused.error_code == "category_not_chosen"
    assert "Policy & Regulations" not in str(posted)
