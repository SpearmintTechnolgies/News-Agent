#!/usr/bin/env python3
"""Tests for web search tool."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "database"))

import backlink_db  # noqa: E402
from tools.search.search import (  # noqa: E402
    APPROVED_BACKENDS,
    SearchError,
    SearchResult,
    cache_key,
    dedupe_by_url,
    normalize_results,
    search,
)


class SearchToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_normalize_results_maps_ddgs_fields(self) -> None:
        raw = [{"title": "Guest Post", "href": "https://example.com/guest", "body": "Write for us"}]
        results = normalize_results(raw, source="google")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/guest")
        self.assertEqual(results[0].snippet, "Write for us")
        self.assertEqual(results[0].source, "google")

    def test_dedupe_by_url_strips_trailing_slash(self) -> None:
        results = [
            SearchResult(title="A", url="https://Example.com/page/", snippet="", source="google"),
            SearchResult(title="B", url="https://example.com/page", snippet="", source="google"),
        ]
        deduped = dedupe_by_url(results)
        self.assertEqual(len(deduped), 1)

    @patch("tools.search.search._search_backend")
    def test_search_uses_first_working_backend(self, mock_backend: MagicMock) -> None:
        mock_backend.return_value = [
            {"title": "Crypto Guest Post", "href": "https://blog.example.com/write-for-us", "body": "Submit"},
        ]
        results = search("crypto guest post", limit=5, use_cache=False, db_path=self.db_path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, APPROVED_BACKENDS[0])
        mock_backend.assert_called_once()

    @patch("tools.search.search._search_backend")
    def test_search_falls_back_on_empty_results(self, mock_backend: MagicMock) -> None:
        mock_backend.side_effect = [[], [{"title": "Bing Hit", "href": "https://bing.example.com", "body": "x"}]]
        results = search("fallback query", limit=3, use_cache=False, db_path=self.db_path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "bing")
        self.assertEqual(mock_backend.call_count, 2)

    @patch("tools.search.search._search_backend")
    def test_search_uses_cache_on_second_call(self, mock_backend: MagicMock) -> None:
        mock_backend.return_value = [
            {"title": "Cached", "href": "https://cache.example.com", "body": "cached"},
        ]
        first = search("cached query", limit=3, db_path=self.db_path)
        second = search("cached query", limit=3, db_path=self.db_path)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0].url, second[0].url)
        mock_backend.assert_called_once()

    @patch("tools.search.search._search_backend")
    def test_search_raises_when_all_backends_fail(self, mock_backend: MagicMock) -> None:
        from ddgs.exceptions import DDGSException

        mock_backend.side_effect = DDGSException("No results found.")
        with self.assertRaises(SearchError):
            search("nothing", use_cache=False, db_path=self.db_path)

    def test_cache_key_includes_backend(self) -> None:
        self.assertNotEqual(cache_key("Hello", 10, "google"), cache_key("hello", 10, "bing"))


if __name__ == "__main__":
    unittest.main()
