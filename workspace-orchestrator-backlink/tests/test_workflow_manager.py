#!/usr/bin/env python3
"""Tests for backlink workflow state engine."""
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
import workflow_manager  # noqa: E402
from states import WorkflowState, can_transition  # noqa: E402


class WorkflowManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_create_workflow_starts_new(self) -> None:
        row = workflow_manager.create("WF-TEST-001", db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.NEW.value)
        self.assertEqual(workflow_manager.next_action_for(row.workflow_id, db_path=self.db_path), "discovery")

    def test_valid_transition_new_to_discovered(self) -> None:
        workflow_manager.create("WF-TEST-002", db_path=self.db_path)
        row = workflow_manager.transition(
            "WF-TEST-002",
            WorkflowState.DISCOVERED.value,
            agent="discovery",
            db_path=self.db_path,
        )
        self.assertEqual(row.state, WorkflowState.DISCOVERED.value)
        self.assertEqual(row.current_agent, "discovery")

    def test_invalid_transition_raises(self) -> None:
        workflow_manager.create("WF-TEST-003", db_path=self.db_path)
        with self.assertRaises(ValueError):
            workflow_manager.transition(
                "WF-TEST-003",
                WorkflowState.APPROVED.value,
                db_path=self.db_path,
            )

    def test_audit_log_written(self) -> None:
        workflow_manager.create("WF-TEST-004", db_path=self.db_path)
        workflow_manager.transition(
            "WF-TEST-004",
            WorkflowState.DISCOVERED.value,
            agent="discovery",
            db_path=self.db_path,
        )
        logs = backlink_db.list_logs("WF-TEST-004", db_path=self.db_path)
        steps = [log["step"] for log in logs]
        self.assertIn("create", steps)
        self.assertIn("transition", steps)

    def test_agent_result_success(self) -> None:
        workflow_manager.create("WF-TEST-005", db_path=self.db_path)
        workflow_manager.transition(
            "WF-TEST-005",
            WorkflowState.DISCOVERED.value,
            db_path=self.db_path,
        )
        result = workflow_manager.AgentResult(
            success=True,
            workflow_id="WF-TEST-005",
            step="qualification",
            data={"final_score": 8.5},
        )
        row = workflow_manager.apply_agent_result(
            result,
            success_state=WorkflowState.QUALIFIED.value,
            db_path=self.db_path,
        )
        self.assertEqual(row.state, WorkflowState.QUALIFIED.value)

    def test_can_transition_matrix(self) -> None:
        self.assertTrue(can_transition(WorkflowState.NEW, WorkflowState.DISCOVERED))
        self.assertFalse(can_transition(WorkflowState.NEW, WorkflowState.APPROVED))


if __name__ == "__main__":
    unittest.main()
