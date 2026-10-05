from __future__ import annotations

from pathlib import Path

from newsagent_v2.publication.master_index import MasterIndexStore, sync_wordpress_posts
from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.html_formatter import ArticleHtmlFormatter


def _post(post_id: int, title: str, modified: str, *, tags: list[str] | None = None) -> dict:
    return {
        "id": post_id,
        "link": f"https://news.example/posts/{post_id}",
        "title": {"rendered": f"<em>{title}</em>"},
        "date_gmt": "2025-01-01T00:00:00",
        "modified_gmt": modified,
        "categories": ["markets"],
        "tags": tags or ["bitcoin"],
        "meta": {"topic": "markets", "focus_keyphrase": title.lower()},
    }


def test_bootstrap_follows_pages_until_empty_and_persists_compact_metadata(tmp_path: Path):
    store = MasterIndexStore(tmp_path)
    calls: list[str] = []
    pages = {
        "1": [_post(1, "Bitcoin Markets", "2025-01-02T00:00:00")],
        "2": [_post(2, "Ethereum Markets", "2025-01-03T00:00:00", tags=["ethereum"])],
        "3": [],
    }

    def transport(method, url, **kwargs):
        calls.append(url)
        page = url.split("page=")[1].split("&")[0]
        return {"ok": True, "payload": pages[page]}

    result = sync_wordpress_posts(
        config=WordPressConfig("https://news.example", "user", "pass"),
        transport=transport,
        store=store,
    )

    assert result["ok"] is True
    assert result["bootstrap"] is True
    assert result["pages"] == 2
    assert len(calls) == 3
    record = store.search_wordpress("Bitcoin", limit=1)[0]
    assert record.wp_post_id == 1
    assert record.title == "Bitcoin Markets"
    assert record.categories == ["markets"]
    assert record.tags == ["bitcoin"]
    assert record.seo_metadata["focus_keyphrase"] == "bitcoin markets"
    assert store.load_sync_state()["bootstrap_complete"] is True


def test_incremental_sync_uses_modified_cursor_and_updates_by_wp_id(tmp_path: Path):
    store = MasterIndexStore(tmp_path)
    first = _post(7, "Old title", "2025-01-02T00:00:00")
    store.record_wordpress_post(first)
    store.save_sync_state({
        "bootstrap_complete": True,
        "last_modified_at": "2025-01-02T00:00:00",
        "last_sync_at": "2025-01-02T00:00:00",
    })
    urls: list[str] = []

    def transport(method, url, **kwargs):
        urls.append(url)
        if "&page=1&" in url:
            return {"ok": True, "payload": [_post(7, "New title", "2025-01-04T00:00:00"), _post(8, "New story", "2025-01-05T00:00:00")]}
        return {"ok": True, "payload": []}

    result = sync_wordpress_posts(
        config=WordPressConfig("https://news.example", "user", "pass"),
        transport=transport,
        store=store,
    )

    assert result["bootstrap"] is False
    assert "modified_after=2025-01-02T00%3A00%3A00" in urls[0]
    assert len(store.list_records()) == 2
    assert store.search_wordpress("New title", limit=1)[0].wp_post_id == 7
    assert store.search_wordpress("New title", limit=1)[0].title == "New title"


def test_internal_retrieval_uses_local_index_without_rest_search(tmp_path: Path):
    store = MasterIndexStore(tmp_path)
    store.record_wordpress_post(_post(1, "Bitcoin regulation", "2025-01-02T00:00:00"))
    store.record_wordpress_post(_post(2, "Bitcoin market", "2025-01-03T00:00:00"))
    rest_calls: list[str] = []

    def transport(method, url, **kwargs):
        rest_calls.append(url)
        return {"ok": False, "payload": []}

    formatter = ArticleHtmlFormatter(
        WordPressConfig("https://news.example", "user", "pass"),
        transport,
        master_index=store,
    )
    result = formatter.format_article(
        "A story.", [], topic="bitcoin", entities=["regulation"], max_read_also=1
    )

    assert len(result.internal_links) == 1
    assert result.internal_links[0]["id"] == 1
    assert rest_calls == []


def test_incremental_capability_fallback_still_paginates(tmp_path: Path):
    store = MasterIndexStore(tmp_path)
    store.save_sync_state({"bootstrap_complete": True, "last_modified_at": "2025-01-01T00:00:00"})
    calls: list[str] = []

    def transport(method, url, **kwargs):
        calls.append(url)
        if "modified_after=" in url:
            return {"ok": False, "error": "unsupported parameter"}
        page = url.split("page=")[1].split("&")[0]
        return {"ok": True, "payload": [_post(10, "Fallback story", "2025-02-01T00:00:00")] if page == "1" else []}

    result = sync_wordpress_posts(
        config=WordPressConfig("https://news.example", "user", "pass"),
        transport=transport,
        store=store,
    )

    assert result["ok"] is True
    assert len(calls) == 3
    assert store.search_wordpress("Fallback", limit=1)[0].wp_post_id == 10
