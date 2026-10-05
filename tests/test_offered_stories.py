"""A later /make skips stories the previous /make already offered."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from newsagent_v2.control.offered_stories import OFFER_BATCH, OfferedStories


def _event(title: str, url: str):
    return SimpleNamespace(
        canonical_title=title,
        reports=[SimpleNamespace(url=url)],
    )


def test_batch_size_is_ten():
    assert OFFER_BATCH == 10


def test_same_headline_or_shared_url_is_already_offered(tmp_path):
    store = OfferedStories([], path=tmp_path / "offered.json")
    first = _event("Bitcoin tests $87,363 resistance", "https://news.example/btc?utm_source=rss")
    store.remember([first])

    reloaded = OfferedStories.load(tmp_path / "offered.json")
    assert reloaded.already(_event("Bitcoin tests $87,363 resistance", "https://other.example/a"))
    assert reloaded.already(_event("A different headline", "https://news.example/btc"))
    assert not reloaded.already(_event("SEC delays a different fund", "https://sec.example/other"))


def test_offered_stories_expire(tmp_path):
    store = OfferedStories([], path=tmp_path / "offered.json")
    story = _event("Old bitcoin headline", "https://news.example/old")
    stale = datetime.now(timezone.utc) - timedelta(hours=49)
    store.remember([story], now=stale)

    assert not OfferedStories.load(tmp_path / "offered.json").already(story)


def test_next_batch_keeps_stories_that_were_not_offered(tmp_path):
    store = OfferedStories([], path=tmp_path / "offered.json")
    offered = [_event(f"Story {n}", f"https://news.example/{n}") for n in range(10)]
    store.remember(offered)

    later = OfferedStories.load(tmp_path / "offered.json")
    skipped = [event for event in offered if later.already(event)]
    fresh = _event("Story 10", "https://news.example/10")
    assert len(skipped) == 10
    assert not later.already(fresh)
