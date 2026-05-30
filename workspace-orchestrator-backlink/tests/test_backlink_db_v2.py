#!/usr/bin/env python3
"""Tests for v2 database schema and helpers."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "database"))

import backlink_db  # noqa: E402


class BacklinkDbV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_campaign_opportunity_workflow_chain(self) -> None:
        campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )
        opp, wf = backlink_db.create_opportunity_and_workflow(
            campaign_id,
            "https://Example.com/write-for-us/",
            "WF-V2-001",
            title="Write For Us",
            db_path=self.db_path,
        )
        self.assertEqual(opp.normalized_url, "https://example.com/write-for-us")
        self.assertEqual(wf.opportunity_id, opp.id)
        self.assertEqual(wf.campaign_id, campaign_id)

    def test_duplicate_opportunity_rejected(self) -> None:
        campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )
        backlink_db.create_opportunity_and_workflow(
            campaign_id,
            "https://example.com/write-for-us",
            "WF-V2-002",
            db_path=self.db_path,
        )
        with self.assertRaises(ValueError):
            backlink_db.create_opportunity(
                campaign_id,
                "https://example.com/write-for-us/",
                db_path=self.db_path,
            )

    def test_find_opportunity_by_url_hash(self) -> None:
        campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )
        backlink_db.create_opportunity(
            campaign_id,
            "https://blog.example.com/guest-post",
            db_path=self.db_path,
        )
        found = backlink_db.find_opportunity_by_url_hash(
            campaign_id,
            backlink_db.url_hash("https://blog.example.com/guest-post/"),
            db_path=self.db_path,
        )
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.domain, "blog.example.com")

    def test_set_pending_approval_sets_expiry(self) -> None:
        campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )
        _, wf = backlink_db.create_opportunity_and_workflow(
            campaign_id,
            "https://example.com/page",
            "WF-V2-003",
            db_path=self.db_path,
        )
        updated = backlink_db.set_pending_approval(wf.workflow_id, db_path=self.db_path)
        self.assertIsNotNone(updated.pending_approval_at)
        self.assertIsNotNone(updated.approval_expires_at)

    def test_parsed_page_cache(self) -> None:
        url = "https://example.com/write-for-us"
        backlink_db.save_parsed_page(
            url,
            title="Guest Post",
            content_hash="abc123",
            signals={"guest_post_language": True},
            db_path=self.db_path,
        )
        cached = backlink_db.get_parsed_page(url, db_path=self.db_path)
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached["title"], "Guest Post")
        self.assertTrue(cached["signals"]["guest_post_language"])

    def test_url_hash_normalizes_trailing_slash(self) -> None:
        h1 = backlink_db.url_hash("https://Example.com/page/")
        h2 = backlink_db.url_hash("https://example.com/page")
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
