"""The opening menu filters the story list, and the card prices every attempt."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from newsagent_v2.control.story_picker import (
    category_queries,
    menu_keyboard,
    order_stories,
    story_matches,
)
from newsagent_v2.v5_generation.article_spend import record_text_spend, total_text_spend


def _event(title: str, topic: str = "", hours: float = 2, published_at: str | None = None):
    reports = []
    if published_at:
        reports = [SimpleNamespace(title=title, topics=[], published_at=published_at)]
    return SimpleNamespace(canonical_title=title, topic=topic, reports=reports, age_hours=hours)


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_menu_offers_trend_last_six_hours_and_categories():
    buttons = [button for row in menu_keyboard()["inline_keyboard"] for button in row]
    labels = [button["text"] for button in buttons]
    callbacks = [button["callback_data"] for button in buttons]
    assert labels[:2] == ["TRENDING", "6 HOURS"]
    assert callbacks[:2] == ["pick:trend", "pick:h6"]
    assert "Bitcoin" in labels and "Stablecoins" in labels
    assert all("24" not in label for label in labels)


def test_category_and_age_filters():
    bitcoin = _event("Strategy buys more Bitcoin", hours=10)
    sec = _event("SEC delays the ETF decision", topic="regulation", hours=3)
    assert story_matches(bitcoin, "trend")
    assert story_matches(bitcoin, "bitcoin")
    assert not story_matches(bitcoin, "h6")
    assert story_matches(sec, "h6")
    assert story_matches(sec, "regulation")
    assert not story_matches(sec, "defi")
    assert not story_matches(_event("Bitcoin price swings again"), "markets")
    assert story_matches(_event("Bitcoin ETF inflows hit a record"), "markets")
    # The 6-hour button uses the earliest publisher, not a later copy.
    early = _event("SEC opens a case", hours=1, published_at=_iso_hours_ago(10))
    late = _event("SEC closes a case", hours=20, published_at=_iso_hours_ago(2))
    assert not story_matches(early, "h6")
    assert story_matches(late, "h6")


def test_category_search_queries_skip_the_time_buttons():
    assert category_queries("trend") == ()
    assert category_queries("h6") == ()
    assert "crypto AI" in category_queries("ai")
    assert category_queries("bitcoin") == ("bitcoin",)


def test_category_list_is_newest_first():
    older = _event("Bitcoin miners expand", published_at=_iso_hours_ago(30))
    newer = _event("Bitcoin miners pause", published_at=_iso_hours_ago(4))
    ordered = order_stories([older, newer], "bitcoin")
    assert [event.canonical_title for event in ordered] == [
        "Bitcoin miners pause",
        "Bitcoin miners expand",
    ]
    ranked = order_stories([older, newer], "trend")
    assert ranked[0] is older


def test_each_call_is_priced_as_content_or_orchestration(tmp_path):
    from newsagent_v2.v5_generation.depth_cost_helpers import build_review_cost_text

    record_text_spend(
        "evt-2",
        {
            "provider": "bedrock-mantle",
            "log": [
                {"stage": "draft", "prompt_tokens": 20000, "completion_tokens": 5000},
                {"stage": "revise_1", "prompt_tokens": 14640, "completion_tokens": 2630},
            ],
        },
        tmp_path,
    )
    total = total_text_spend("evt-2", tmp_path)
    assert total["prompt_tokens"] == 34640
    assert total["completion_tokens"] == 7630
    assert [row["kind"] for row in total["attempts"]] == ["content", "orchestration"]
    text = build_review_cost_text(
        text_usage=total,
        image_usage={
            "requests": 1,
            "accumulated_cost_usd": 0.067,
            "provider_reported_usage": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 1120,
                "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
            },
        },
        environ={
            "NEWSAGENT_V2_KIMI_PRICING_VERIFIED": "true",
            "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK": "3.30",
            "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK": "16.50",
        },
        escape_html=lambda value: value,
    )
    assert "CONTENT — 1 call" in text
    assert "ORCHESTRATION — 1 call" in text
    assert "revise 1" in text
    assert "IMAGE — 1 call — $0.0670 USD" in text
    assert "1. Input: 100 | Output: 1120" in text
    assert "TOTAL COST — $" in text
    assert "unavailable" not in text.split("TOTAL COST")[-1]


def test_card_total_adds_the_discarded_rewrite_and_the_later_revise(tmp_path):
    record_text_spend("evt-1", {"prompt_tokens": 1000, "completion_tokens": 400, "calls": 2}, tmp_path)
    record_text_spend("evt-1", {"prompt_tokens": 800, "completion_tokens": 300, "calls": 1}, tmp_path)
    total = total_text_spend("evt-1", tmp_path)
    assert total["prompt_tokens"] == 1800
    assert total["completion_tokens"] == 700
    assert total["total_tokens"] == 2500
    assert total["calls"] == 3
