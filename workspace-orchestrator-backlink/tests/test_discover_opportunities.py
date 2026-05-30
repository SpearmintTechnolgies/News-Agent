#!/usr/bin/env python3
"""Tests for discover_opportunities skill."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))
sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_driver  # noqa: E402
import workflow_manager  # noqa: E402
from discover_opportunities import (  # noqa: E402
    discover_from_search,
    enrich_workflow_opportunity,
    fetch_and_parse_url,
)
from states import WorkflowState  # noqa: E402
from tools.page_fetch.page_fetch import PageFetchResult  # noqa: E402
from tools.search.search import SearchResult  # noqa: E402

SAMPLE_HTML = """
<html><head><title>Write For Us - Crypto Blog</title></head>
<body><p>We welcome guest post submissions.</p>
<form method="post"><textarea name="body"></textarea></form></body></html>
"""


class DiscoverOpportunitiesTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BACKLINK_TELEGRAM_DRY_RUN"] = "1"
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)
        self.campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        self._tmpdir.cleanup()

    @patch("tools.page_fetch.page_fetch.page_fetch")
    def test_fetch_and_parse_uses_cache_on_second_call(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = PageFetchResult(
            html=SAMPLE_HTML,
            status=200,
            final_url="https://example.com/write-for-us",
        )
        first = fetch_and_parse_url("https://example.com/write-for-us", db_path=self.db_path)
        second = fetch_and_parse_url("https://example.com/write-for-us", db_path=self.db_path)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertTrue(first["signals"]["guest_post_language"])
        mock_fetch.assert_called_once()

    @patch("discover_opportunities.fetch_and_parse_url")
    @patch("discover_opportunities.search")
    def test_discover_creates_workflow_for_new_url(
        self,
        mock_search: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        mock_search.return_value = [
            SearchResult(
                title="Guest Post Guidelines",
                url="https://blog.example.com/write-for-us",
                snippet="Submit your crypto article",
            )
        ]
        mock_fetch.return_value = {
            "url": "https://blog.example.com/write-for-us",
            "cached": False,
            "title": "Write For Us",
            "signals": {"guest_post_language": True},
        }

        result = discover_from_search(
            self.campaign_id,
            queries=["cryptography guest post"],
            limit_per_query=3,
            db_path=self.db_path,
        )

        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["skipped_duplicates"], [])
        wf_id = result["created"][0]["workflow_id"]
        row = workflow_manager.load(wf_id, db_path=self.db_path)
        self.assertEqual(row.state, "NEW")
        opp = backlink_db.get_opportunity(result["created"][0]["opportunity_id"], db_path=self.db_path)
        assert opp is not None
        self.assertEqual(opp.domain, "blog.example.com")
        context = json.loads(opp.context_json or "{}")
        self.assertTrue(context["discovery_signals"]["guest_post_language"])

    @patch("discover_opportunities.fetch_and_parse_url")
    @patch("discover_opportunities.search")
    def test_discover_skips_duplicate_url(
        self,
        mock_search: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            "https://blog.example.com/write-for-us",
            "WF-EXISTING",
            db_path=self.db_path,
        )
        mock_search.return_value = [
            SearchResult(
                title="Guest Post Guidelines",
                url="https://blog.example.com/write-for-us/",
                snippet="Submit your crypto article",
            )
        ]
        mock_fetch.return_value = {
            "url": "https://blog.example.com/write-for-us",
            "cached": True,
            "title": "Write For Us",
            "signals": {},
        }

        result = discover_from_search(
            self.campaign_id,
            queries=["cryptography guest post"],
            db_path=self.db_path,
        )

        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["skipped_duplicates"]), 1)
        self.assertEqual(result["skipped_duplicates"][0]["reason"], "duplicate_url_hash")

    @patch("discover_opportunities.fetch_and_parse_url")
    def test_enrich_workflow_opportunity(self, mock_fetch: MagicMock) -> None:
        opp, wf = backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            "https://example.com/write-for-us",
            "WF-ENRICH-001",
            db_path=self.db_path,
        )
        mock_fetch.return_value = {
            "url": opp.url,
            "cached": False,
            "title": "Write For Us - Crypto Blog",
            "signals": {"guest_post_language": True, "has_form": True},
        }

        result = enrich_workflow_opportunity(wf.workflow_id, db_path=self.db_path)
        self.assertTrue(result.success)
        self.assertEqual(result.data["signals"]["guest_post_language"], True)

        step = workflow_driver.run_step(wf.workflow_id, db_path=self.db_path)
        self.assertEqual(step.status, "ok")
        row = workflow_manager.load(wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.DISCOVERED.value)


if __name__ == "__main__":
    unittest.main()
