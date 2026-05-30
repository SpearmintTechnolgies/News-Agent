#!/usr/bin/env python3
"""Tests for Step 11 hardening — expiry, idempotency."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))
sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))
sys.path.insert(0, str(_ROOT / "tools" / "telegram"))

import backlink_db  # noqa: E402
import workflow_driver  # noqa: E402
import workflow_manager  # noqa: E402
from handle_backlink_callback import handle_approve  # noqa: E402
from publish_backlink import publish_backlink  # noqa: E402
from send_backlink_card import send_approval_card  # noqa: E402
from states import WorkflowState  # noqa: E402
from validate_image import write_test_jpeg  # noqa: E402


class HardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BACKLINK_TELEGRAM_DRY_RUN"] = "1"
        os.environ["BACKLINK_PUBLISH_DRY_RUN"] = "1"
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
            "WF-HARD-001",
            title="Write For Us",
            db_path=self.db_path,
        )
        backlink_db.update_opportunity_fields(
            self.opp.id,
            placement_type="guest_post",
            final_score=72.0,
            db_path=self.db_path,
        )
        backlink_db.save_draft(self.wf.workflow_id, "Title: Test\n\nBody.", db_path=self.db_path)
        self.image_path = os.path.join(self._tmpdir.name, "feature.jpg")
        write_test_jpeg(self.image_path)
        backlink_db.save_content_asset(
            self.wf.workflow_id,
            "Title: Test\n\nBody.",
            target_link="https://cryptography.com",
            image_local_path=self.image_path,
            db_path=self.db_path,
        )
        for state in (
            WorkflowState.DISCOVERED.value,
            WorkflowState.QUALIFIED.value,
            WorkflowState.PLACEMENT_CHECK.value,
            WorkflowState.DRAFTED.value,
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
        self._tmpdir.cleanup()

    def test_approval_card_shows_expiry(self) -> None:
        result = send_approval_card(
            self.wf.workflow_id,
            db_path=self.db_path,
            dry_run=False,
            sender=lambda payload: {
                "telegram_chat_id": "-100123",
                "telegram_message_id": 1,
            },
        )
        self.assertNotIn("Expires: not set", result["caption"])
        self.assertIn("Expires:", result["caption"])

    def test_send_approval_card_is_idempotent(self) -> None:
        send_approval_card(
            self.wf.workflow_id,
            db_path=self.db_path,
            dry_run=True,
        )
        workflow_manager.transition(
            self.wf.workflow_id,
            WorkflowState.PENDING_APPROVAL.value,
            agent="test-setup",
            db_path=self.db_path,
        )
        second = send_approval_card(
            self.wf.workflow_id,
            db_path=self.db_path,
            dry_run=True,
        )
        self.assertTrue(second.get("skipped"))

    def test_expire_stale_approvals_archives(self) -> None:
        send_approval_card(self.wf.workflow_id, db_path=self.db_path, dry_run=True)
        workflow_manager.transition(
            self.wf.workflow_id,
            WorkflowState.PENDING_APPROVAL.value,
            agent="test-setup",
            db_path=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE workflows SET approval_expires_at = datetime('now', '-1 hour') WHERE workflow_id = ?",
            (self.wf.workflow_id,),
        )
        conn.commit()
        conn.close()
        expired = backlink_db.expire_stale_approvals(db_path=self.db_path)
        self.assertIn(self.wf.workflow_id, expired)
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)

    def test_expired_approval_cannot_be_approved(self) -> None:
        send_approval_card(self.wf.workflow_id, db_path=self.db_path, dry_run=True)
        workflow_manager.transition(
            self.wf.workflow_id,
            WorkflowState.PENDING_APPROVAL.value,
            agent="test-setup",
            db_path=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE workflows SET approval_expires_at = datetime('now', '-1 hour') WHERE workflow_id = ?",
            (self.wf.workflow_id,),
        )
        conn.commit()
        conn.close()
        backlink_db.expire_stale_approvals(db_path=self.db_path)
        with self.assertRaises(ValueError):
            handle_approve(self.wf.workflow_id, db_path=self.db_path)

    def test_publish_is_idempotent(self) -> None:
        for state in (
            WorkflowState.PENDING_APPROVAL.value,
            WorkflowState.APPROVED.value,
        ):
            workflow_manager.transition(
                self.wf.workflow_id,
                state,
                agent="test-setup",
                db_path=self.db_path,
            )
        first = publish_backlink(self.wf.workflow_id, db_path=self.db_path, dry_run=True)
        second = publish_backlink(self.wf.workflow_id, db_path=self.db_path, dry_run=True)
        self.assertTrue(second.detail.get("idempotent"))
        self.assertEqual(first.published_url, second.published_url)


if __name__ == "__main__":
    unittest.main()
