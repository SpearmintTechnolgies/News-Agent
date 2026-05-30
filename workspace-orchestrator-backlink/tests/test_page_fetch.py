#!/usr/bin/env python3
"""Tests for Playwright page fetch tool."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tools.page_fetch.page_fetch import page_fetch  # noqa: E402


class PageFetchTests(unittest.TestCase):
    @patch("playwright.sync_api.sync_playwright")
    def test_page_fetch_returns_html_and_status(self, mock_sync_playwright: MagicMock) -> None:
        mock_page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_page.goto.return_value = mock_response
        mock_page.content.return_value = "<html><body>hello</body></html>"
        mock_page.url = "https://example.com/final"

        mock_browser = MagicMock()
        mock_browser.new_page.return_value = mock_page

        mock_playwright = MagicMock()
        mock_playwright.chromium.launch.return_value = mock_browser
        mock_sync_playwright.return_value.__enter__.return_value = mock_playwright

        result = page_fetch("https://example.com")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.final_url, "https://example.com/final")
        self.assertIn("hello", result.html)
        mock_page.goto.assert_called_once()


if __name__ == "__main__":
    unittest.main()
