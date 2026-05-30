#!/usr/bin/env python3
"""Tests for publish and verify skills."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))
sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_driver  # noqa: E402
import workflow_manager  # noqa: E402
from publish_backlink import publish_backlink  # noqa: E402
from states import WorkflowState  # noqa: E402
from verify_backlink import link_visible_in_html, verify_backlink  # noqa: E402


class PublishVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BACKLINK_TELEGRAM_DRY_RUN"] = "1"
        os.environ["BACKLINK_PUBLISH_DRY_RUN"] = "1"
        os.environ["BACKLINK_PHASE"] = "2"
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
            "WF-PUB-001",
            title="Write For Us",
            db_path=self.db_path,
        )
        backlink_db.update_opportunity_fields(
            self.opp.id,
            placement_type="guest_post",
            final_score=72.0,
            db_path=self.db_path,
        )
        backlink_db.save_draft(
            self.wf.workflow_id,
            "Title: Test Draft\n\nBody with cryptography.com mention.",
            db_path=self.db_path,
        )
        for state in (
            WorkflowState.DISCOVERED.value,
            WorkflowState.QUALIFIED.value,
            WorkflowState.PLACEMENT_CHECK.value,
            WorkflowState.DRAFTED.value,
            WorkflowState.PENDING_APPROVAL.value,
            WorkflowState.APPROVED.value,
        ):
            workflow_manager.transition(
                self.wf.workflow_id,
                state,
                agent="test-setup",
                db_path=self.db_path,
            )

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        os.environ.pop("BACKLINK_PUBLISH_DRY_RUN", None)
        os.environ.pop("BACKLINK_PHASE", None)
        self._tmpdir.cleanup()

    def test_dry_run_publish_records_backlink(self) -> None:
        result = publish_backlink(self.wf.workflow_id, db_path=self.db_path, dry_run=True)
        self.assertEqual(result.outcome_state, WorkflowState.PUBLISHED.value)
        self.assertTrue(result.dry_run)
        row = backlink_db.get_backlink(self.wf.workflow_id, db_path=self.db_path)
        assert row is not None
        self.assertIn("backlink_dry_run", row.published_url or "")

    def test_publish_driver_reaches_published(self) -> None:
        step = workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(step.action, "publisher")
        self.assertEqual(step.status, "ok")
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.PUBLISHED.value)

    def test_verify_dry_run_finds_target_link(self) -> None:
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        result = verify_backlink(self.wf.workflow_id, db_path=self.db_path)
        self.assertTrue(result.verified)
        self.assertEqual(result.outcome_state, WorkflowState.VERIFIED.value)

    def test_link_visible_in_html(self) -> None:
        html = '<a href="https://cryptography.com/blog">read</a>'
        self.assertEqual(link_visible_in_html(html, "cryptography.com"), 1)

    def test_post_approval_pipeline_to_verified(self) -> None:
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)  # publish
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)  # verify_fetch
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)  # verifier
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.VERIFIED.value)


if __name__ == "__main__":
    unittest.main()
