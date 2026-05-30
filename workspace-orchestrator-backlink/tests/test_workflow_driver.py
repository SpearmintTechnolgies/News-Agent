#!/usr/bin/env python3
"""Tests for workflow driver stubs."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_driver  # noqa: E402
import workflow_manager  # noqa: E402
from states import WorkflowState  # noqa: E402


class WorkflowDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BACKLINK_TELEGRAM_DRY_RUN"] = "1"
        os.environ["BACKLINK_SKIP_IMAGE_GEN"] = "1"
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)
        self.campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )
        self.opp, self.wf = backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            "https://example.com/write-for-us",
            "WF-DRIVER-001",
            title="Write For Us",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        os.environ.pop("BACKLINK_SKIP_IMAGE_GEN", None)
        self._tmpdir.cleanup()

    def test_single_discovery_step(self) -> None:
        result = workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.action, "discovery")
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.DISCOVERED.value)

    def test_run_until_pending_approval(self) -> None:
        results = workflow_driver.run_until_wait(self.wf.workflow_id, db_path=self.db_path)
        self.assertTrue(len(results) >= 5)
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.PENDING_APPROVAL.value)
        self.assertIsNotNone(row.approval_expires_at)
        last = results[-1]
        self.assertEqual(last.status, "wait")

    def test_wait_when_no_action(self) -> None:
        workflow_driver.run_until_wait(self.wf.workflow_id, db_path=self.db_path)
        result = workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(result.status, "wait")
        self.assertIsNone(result.action)


if __name__ == "__main__":
    unittest.main()
