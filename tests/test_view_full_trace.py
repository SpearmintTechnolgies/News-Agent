"""Trace VIEW FULL callback end-to-end."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler


def test_view_full_parsing():
    """Test callback parsing."""
    callback = "view_full:evt-fec17bd1:v2:article"

    handler = V5ReviewCallbackHandler(None, None, MagicMock())
    parsed = handler.parse_callback(callback)

    print(f"Input: {callback}")
    print(f"Parsed: {parsed}")

    assert parsed is not None, "Should parse successfully"
    assert parsed["action"] == "view_full"
    assert parsed["event_id"] == "evt-fec17bd1"
    assert parsed["version"] == "v2"
    assert parsed["extra"] == "article"

    print("\n[PASS] Parser accepts view_full callback")


def test_view_full_handler():
    """Test handle_view_full is called and returns correct action."""

    # Mock version store with article V2
    mock_version_store = MagicMock()
    mock_version_store.get_article.return_value = {
        "article": {
            "headline": "Test Headline",
            "article_body": "Test body content...",
        },
        "version": "v2",
    }

    handler = V5ReviewCallbackHandler(None, None, mock_version_store)

    # Call handle_view_full directly
    result = handler.handle_view_full("evt-fec17bd1", "v2", "article")

    print(f"\nhandle_view_full('evt-fec17bd1', 'v2', 'article')")
    print(f"Result: {result}")

    assert result["ok"] is True
    assert result["action"] == "view_full_article"
    assert result["event_id"] == "evt-fec17bd1"
    assert result["version"] == "v2"
    assert result["headline"] == "Test Headline"
    assert result["body"] == "Test body content..."

    print("\n[PASS] handle_view_full returns correct action")


def test_image_view_full():
    """Test handle_view_full for image."""
    from pathlib import Path

    # Create a real Path object that exists() check will work with
    test_path = Path(__file__)  # This file exists

    mock_version_store = MagicMock()
    mock_version_store.get_image_path.return_value = test_path

    handler = V5ReviewCallbackHandler(None, None, mock_version_store)

    result = handler.handle_view_full("evt-fec17bd1", "v2", "image")

    print(f"\nhandle_view_full('evt-fec17bd1', 'v2', 'image')")
    print(f"Result: {result}")

    assert result["ok"] is True
    assert result["action"] == "view_full_image"

    print("\n[PASS] handle_view_full returns view_full_image for image type")


def test_full_dispatch():
    """Test full dispatch through handle()."""

    callback = "view_full:evt-fec17bd1:v2:article"

    mock_version_store = MagicMock()
    mock_version_store.get_article.return_value = {
        "article": {"headline": "H", "article_body": "B"},
    }

    handler = V5ReviewCallbackHandler(None, None, mock_version_store)

    result = handler.handle(callback, reviewer="user")

    print(f"\nhandler.handle('{callback}')")
    print(f"Result: {result}")

    assert result["ok"] is True
    assert result["action"] == "view_full_article"

    print("\n[PASS] Full dispatch works")


if __name__ == "__main__":
    print("=" * 70)
    print("VIEW FULL CALLBACK TRACE TESTS")
    print("=" * 70)

    test_view_full_parsing()
    test_view_full_handler()
    test_image_view_full()
    test_full_dispatch()

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
