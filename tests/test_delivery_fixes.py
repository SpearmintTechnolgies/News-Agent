"""Test delivery fixes for VIEW FULL and image send."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def test_compact_summary_sends_image_when_exists():
    """BUG 2: Image must be sent if it exists, regardless of revision status."""
    from newsagent_v2.v5_generation.telegram_delivery import send_compact_revision_summary

    # Mock client
    mock_client = MagicMock()
    mock_client.send_message.return_value = {"ok": True, "message_id": 100}

    # Mock _send_image to verify it's called
    with patch("newsagent_v2.v5_generation.telegram_delivery._send_image") as mock_send_img:
        mock_send_img.return_value = {"ok": True, "message_id": 200}

        # Mock version store with existing image
        mock_vs = MagicMock()
        mock_article = {
            "article": {
                "headline": "Test Headline",
                "article_body": "Test body content here for preview...",
            },
            "version": "v2",
            "article_hash": "abc123",
        }
        mock_vs.get_article.return_value = mock_article
        mock_vs.get_image_path.return_value = REPO / "test_image.png"  # Need an existing path

        # Create a temp file that exists for the test
        test_img = REPO / "test_image.png"
        test_img.write_bytes(b"fake image data")

        try:
            mock_config = MagicMock()
            mock_config.test_chat_id = "12345"

            # Call with image_revised=False (V2 is current, not newly revised)
            result = send_compact_revision_summary(
                client=mock_client,
                config=mock_config,
                version_store=mock_vs,
                event_id="evt-test",
                canonical_title="Test",
                article_version="v2",
                image_version="v2",
                article_revised=False,
                image_revised=False,  # Key test: NOT revised
            )

            # Assertions
            assert mock_send_img.called, "_send_image MUST be called even when image_revised=False"
            assert result.get("ok"), f"Result should be ok: {result}"

            # Should have message_ids for article AND image
            message_ids = result.get("message_ids", [])
            assert len(message_ids) >= 2, f"Should have article + image IDs: {message_ids}"

            print(f"[PASS] Image sent even when NOT revised")
            print(f"  _send_image called: {mock_send_img.called}")
            print(f"  message_ids: {message_ids}")

        finally:
            test_img.unlink(missing_ok=True)


def test_compact_summary_error_when_image_missing():
    """Image missing should show error, not regenerate."""
    from newsagent_v2.v5_generation.telegram_delivery import send_compact_revision_summary

    mock_client = MagicMock()
    mock_client.send_message.return_value = {"ok": True, "message_id": 100}

    with patch("newsagent_v2.v5_generation.telegram_delivery._send_image") as mock_send_img:
        mock_vs = MagicMock()
        mock_vs.get_article.return_value = {
            "article": {"headline": "Test", "article_body": "Body"},
        }
        # Image path is None (doesn't exist)
        mock_vs.get_image_path.return_value = None

        mock_config = MagicMock()
        mock_config.test_chat_id = "12345"

        # Capture print output
        import io
        captured = io.StringIO()

        with patch("sys.stdout", new=captured):
            result = send_compact_revision_summary(
                client=mock_client,
                config=mock_config,
                version_store=mock_vs,
                event_id="evt-test",
                canonical_title="Test",
                article_version="v2",
                image_version="v2",
                article_revised=False,
                image_revised=False,
            )

        output = captured.getvalue()

        # Should NOT call _send_image
        assert not mock_send_img.called, "Should not call _send_image when image missing"

        # Should print error
        assert "[ERROR]" in output or "not found" in output.lower(), f"Should print error: {output}"

        print(f"[PASS] Missing image shows error without regeneration")
        print(f"  Output: {output[:100]}...")


if __name__ == "__main__":
    print("=" * 70)
    print("DELIVERY FIXES TESTS")
    print("=" * 70 + "\n")

    test_compact_summary_sends_image_when_exists()
    print()
    test_compact_summary_error_when_image_missing()

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
