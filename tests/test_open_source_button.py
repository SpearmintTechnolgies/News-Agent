"""Test OPEN SOURCE button in /make cards."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from newsagent_v2.discovery.event_clusterer import NewsEvent, EventReport
from newsagent_v2.telegram.v5_cards import discovery_keyboard, render_card_from_event


def test_discovery_keyboard_with_url():
    """Keyboard includes OPEN SOURCE when URL provided."""
    keyboard = discovery_keyboard("evt-test", "https://example.com/article")

    rows = keyboard.get("inline_keyboard", [])
    assert len(rows) == 2, f"Expected 2 rows with URL: {rows}"

    # Second row should have OPEN SOURCE
    second_row = rows[1]
    assert len(second_row) == 1
    assert second_row[0].get("text") == "🔗 OPEN SOURCE"
    assert second_row[0].get("url") == "https://example.com/article"

    # First row unchanged
    first_row = rows[0]
    assert any(b.get("text") == "▶ RUN STORY" for b in first_row)
    assert any(b.get("text") == "👁 FOLLOW" for b in first_row)
    assert any(b.get("text") == "🚫 IGNORE" for b in first_row)

    print("[PASS] OPEN SOURCE button added with valid URL")


def test_discovery_keyboard_without_url():
    """Keyboard omits OPEN SOURCE when no URL."""
    keyboard = discovery_keyboard("evt-test", None)

    rows = keyboard.get("inline_keyboard", [])
    assert len(rows) == 1, f"Expected 1 row without URL: {rows}"

    # First row still has buttons
    first_row = rows[0]
    assert any(b.get("text") == "▶ RUN STORY" for b in first_row)

    # No OPEN SOURCE
    for row in rows:
        for btn in row:
            assert "OPEN SOURCE" not in btn.get("text", ""), "Should not have OPEN SOURCE"

    print("[PASS] OPEN SOURCE omitted when no URL")


def test_discovery_keyboard_invalid_url():
    """Keyboard omits OPEN SOURCE for invalid URL schemes."""
    for invalid in ["ftp://example.com", "/local/path", "", "javascript:alert(1)"]:
        keyboard = discovery_keyboard("evt-test", invalid)
        rows = keyboard.get("inline_keyboard", [])

        for row in rows:
            for btn in row:
                assert "OPEN SOURCE" not in btn.get("text", ""), f"Should not have OPEN SOURCE for {invalid}"

    print("[PASS] OPEN SOURCE omitted for invalid URLs")


def test_render_card_includes_url():
    """Full card render includes source URL."""
    # Create event with reports
    event = NewsEvent(
        event_id="evt-test",
        canonical_title="Test Headline",
        topic="Crypto",
    )
    event.reports = [
        EventReport(
            report_id="rpt-1",
            source="CoinDesk",
            source_id="coindesk",
            source_authority=0.8,
            headline="Test",
            url="https://coindesk.com/article",
            published_at="2024-01-01T00:00:00Z",
            retrieved_at="2024-01-01T00:00:00Z",
            description="Desc",
            entities=[],
            raw_item_id="raw-1",
        ),
        EventReport(
            report_id="rpt-2",
            source="TheBlock",
            source_id="theblock",
            source_authority=0.7,
            headline="Test 2",
            url="https://theblock.com/article",
            published_at="2024-01-01T00:00:00Z",
            retrieved_at="2024-01-01T00:00:00Z",
            description="Desc",
            entities=[],
            raw_item_id="raw-2",
        ),
    ]

    card = render_card_from_event(event, rank=1, total_events=5)

    assert card.get("event_id") == "evt-test"
    assert "reply_markup" in card
    assert "First published: 1 Jan 2024, 00:00 UTC" in card["text"]
    assert "Source times:" not in card["text"]

    rows = card["reply_markup"].get("inline_keyboard", [])

    # Should have OPEN SOURCE (highest authority report)
    found_open_source = False
    for row in rows:
        for btn in row:
            if btn.get("text") == "🔗 OPEN SOURCE":
                found_open_source = True
                # Should use highest authority URL
                assert btn.get("url") == "https://coindesk.com/article"

    assert found_open_source, "Should have OPEN SOURCE button"

    print("[PASS] Full card render includes OPEN SOURCE with correct URL")


def test_card_shows_earliest_published_not_retrieved():
    """Slug age uses earliest published_at across sources, not fetch time."""
    event = NewsEvent(
        event_id="evt-age",
        canonical_title="Age Test",
        topic="Crypto",
    )
    event.reports = [
        EventReport(
            report_id="rpt-1",
            source="CoinDesk",
            source_id="coindesk",
            source_authority=0.8,
            headline="Newer",
            url="https://coindesk.com/a",
            published_at="2026-10-03T18:00:00Z",
            retrieved_at="2026-10-04T11:00:00Z",
            description="",
            entities=[],
            raw_item_id="raw-1",
        ),
        EventReport(
            report_id="rpt-2",
            source="Decrypt",
            source_id="decrypt",
            source_authority=0.7,
            headline="Older",
            url="https://decrypt.co/a",
            published_at="2026-10-01T09:30:00Z",
            retrieved_at="2026-10-04T11:05:00Z",
            description="",
            entities=[],
            raw_item_id="raw-2",
        ),
        EventReport(
            report_id="rpt-3",
            source="NoPub",
            source_id="nopub",
            source_authority=0.5,
            headline="Fetch only",
            url="https://example.com/a",
            published_at=None,
            retrieved_at="2026-10-04T12:00:00Z",
            description="",
            entities=[],
            raw_item_id="raw-3",
        ),
    ]

    card = render_card_from_event(event, rank=1, total_events=5)
    assert "First published: 1 Oct 2026, 09:30 UTC" in card["text"]
    assert "Source times:" not in card["text"]
    assert "18:00 UTC" not in card["text"]
    assert "12:00 UTC" not in card["text"]
    assert "11:00 UTC" not in card["text"]
    print("[PASS] Earliest published_at used for news age")


def test_primary_url_property():
    """NewsEvent.primary_url returns highest-authority report URL."""
    event = NewsEvent()
    event.reports = [
        EventReport(
            report_id="rpt-1",
            source="LowAuth",
            source_id="low",
            source_authority=0.5,
            headline="Test",
            url="https://low.com",
            published_at="2024-01-01T00:00:00Z",
            retrieved_at="2024-01-01T00:00:00Z",
            description="Desc",
            entities=[],
            raw_item_id="raw-1",
        ),
        EventReport(
            report_id="rpt-2",
            source="HighAuth",
            source_id="high",
            source_authority=0.95,
            headline="Test",
            url="https://high.com",
            published_at="2024-01-01T00:00:00Z",
            retrieved_at="2024-01-01T00:00:00Z",
            description="Desc",
            entities=[],
            raw_item_id="raw-2",
        ),
    ]

    # Should return highest authority URL
    assert event.primary_url == "https://high.com"

    # No reports
    event2 = NewsEvent()
    assert event2.primary_url is None

    print("[PASS] primary_url returns highest-authority URL")


if __name__ == "__main__":
    print("=" * 70)
    print("OPEN SOURCE BUTTON TESTS")
    print("=" * 70 + "\n")

    test_primary_url_property()
    test_discovery_keyboard_with_url()
    test_discovery_keyboard_without_url()
    test_discovery_keyboard_invalid_url()
    test_render_card_includes_url()

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
