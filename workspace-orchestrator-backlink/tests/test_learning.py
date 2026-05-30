#!/usr/bin/env python3
"""Tests for learning record and backlink report."""
from __future__ import annotations

import json
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
from states import WorkflowState  # noqa: E402
from update_learning_weights import (  # noqa: E402
    compute_weights,
    generate_backlink_report,
    record_workflow_learning,
    save_weights,
    update_learning_weights,
)


class LearningTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["BACKLINK_TELEGRAM_DRY_RUN"] = "1"
        os.environ["BACKLINK_PUBLISH_DRY_RUN"] = "1"
        os.environ["BACKLINK_PHASE"] = "2"
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        self.weights_path = Path(self._tmpdir.name) / "learning_weights.json"
        backlink_db.init_db(self.db_path)
        self.campaign_id = backlink_db.get_or_create_campaign(
            "cryptography.com",
            "cryptography.com",
            db_path=self.db_path,
        )
        self.opp, self.wf = backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            "https://example.com/write-for-us",
            "WF-LEARN-001",
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
            "Title: Test\n\nBody.",
            db_path=self.db_path,
        )
        backlink_db.mark_draft_approved(
            backlink_db.get_latest_draft(self.wf.workflow_id, db_path=self.db_path).id,
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
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.VERIFIED.value)

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        os.environ.pop("BACKLINK_PUBLISH_DRY_RUN", None)
        os.environ.pop("BACKLINK_PHASE", None)
        self._tmpdir.cleanup()

    def test_record_workflow_learning(self) -> None:
        record = record_workflow_learning(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(record.domain, "example.com")
        self.assertTrue(record.approved)
        self.assertTrue(record.published)
        rows = backlink_db.list_learning(db_path=self.db_path)
        self.assertEqual(len(rows), 1)

    def test_update_learning_weights_writes_file(self) -> None:
        result = update_learning_weights(self.wf.workflow_id, db_path=self.db_path)
        save_weights(result["weights"], self.weights_path)
        self.assertTrue(self.weights_path.is_file())
        data = json.loads(self.weights_path.read_text(encoding="utf-8"))
        self.assertIn("domain_boost", data)

    def test_learning_record_step_archives(self) -> None:
        step = workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(step.action, "learning_record")
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)

    def test_generate_backlink_report(self) -> None:
        record_workflow_learning(self.wf.workflow_id, db_path=self.db_path)
        report = generate_backlink_report(db_path=self.db_path)
        self.assertEqual(report["summary"]["learning_rows"], 1)
        self.assertIn("workflows_by_state", report)
        self.assertIn("by_domain", report)

    def test_compute_weights_domain_penalty(self) -> None:
        for i in range(2):
            backlink_db.insert_learning(
                workflow_id=f"WF-FAIL-{i}",
                domain="bad.example.com",
                placement_type="guest_post",
                final_score=40.0,
                approved=True,
                published=False,
                verified=False,
                failure_reason="publish_failed",
                db_path=self.db_path,
            )
        weights = compute_weights(db_path=self.db_path)
        self.assertIn("bad.example.com", weights["domain_penalty"])


if __name__ == "__main__":
    unittest.main()
