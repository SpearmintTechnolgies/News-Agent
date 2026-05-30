#!/usr/bin/env python3
"""Tests for v2 workflow state transitions."""
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
from states import WorkflowState, can_transition, get_next_action  # noqa: E402


class WorkflowStatesV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _create_and_move(self, wid: str, *states: WorkflowState) -> None:
        workflow_manager.create(wid, db_path=self.db_path)
        for state in states:
            workflow_manager.transition(wid, state.value, db_path=self.db_path)

    def test_edit_loop(self) -> None:
        wid = "WF-EDIT-001"
        self._create_and_move(
            wid,
            WorkflowState.DISCOVERED,
            WorkflowState.QUALIFIED,
            WorkflowState.PLACEMENT_CHECK,
            WorkflowState.DRAFTED,
            WorkflowState.PENDING_APPROVAL,
            WorkflowState.EDIT_REQUESTED,
            WorkflowState.DRAFTED,
            WorkflowState.PENDING_APPROVAL,
        )
        row = workflow_manager.load(wid, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.PENDING_APPROVAL.value)

    def test_reject_flow(self) -> None:
        wid = "WF-REJECT-001"
        self._create_and_move(
            wid,
            WorkflowState.DISCOVERED,
            WorkflowState.QUALIFIED,
            WorkflowState.PLACEMENT_CHECK,
            WorkflowState.DRAFTED,
            WorkflowState.PENDING_APPROVAL,
            WorkflowState.REJECTED,
            WorkflowState.ARCHIVED,
        )
        row = workflow_manager.load(wid, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)

    def test_approval_expired_flow(self) -> None:
        wid = "WF-EXPIRE-001"
        self._create_and_move(
            wid,
            WorkflowState.DISCOVERED,
            WorkflowState.QUALIFIED,
            WorkflowState.PLACEMENT_CHECK,
            WorkflowState.DRAFTED,
            WorkflowState.PENDING_APPROVAL,
            WorkflowState.APPROVAL_EXPIRED,
            WorkflowState.ARCHIVED,
        )
        row = workflow_manager.load(wid, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)

    def test_publish_outcomes(self) -> None:
        for outcome in (
            WorkflowState.PUBLISHED,
            WorkflowState.PENDING_MODERATION,
            WorkflowState.SUBMISSION_REJECTED,
        ):
            wid = f"WF-PUB-{outcome.value}"
            self._create_and_move(
                wid,
                WorkflowState.DISCOVERED,
                WorkflowState.QUALIFIED,
                WorkflowState.PLACEMENT_CHECK,
                WorkflowState.DRAFTED,
                WorkflowState.PENDING_APPROVAL,
                WorkflowState.APPROVED,
                WorkflowState.PUBLISHING,
                outcome,
            )
            row = workflow_manager.load(wid, db_path=self.db_path)
            self.assertEqual(row.state, outcome.value)

    def test_verify_pending_then_verified(self) -> None:
        wid = "WF-VERIFY-001"
        self._create_and_move(
            wid,
            WorkflowState.DISCOVERED,
            WorkflowState.QUALIFIED,
            WorkflowState.PLACEMENT_CHECK,
            WorkflowState.DRAFTED,
            WorkflowState.PENDING_APPROVAL,
            WorkflowState.APPROVED,
            WorkflowState.PUBLISHING,
            WorkflowState.PUBLISHED,
            WorkflowState.VERIFYING,
            WorkflowState.VERIFY_PENDING,
            WorkflowState.VERIFYING,
            WorkflowState.VERIFIED,
        )
        row = workflow_manager.load(wid, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.VERIFIED.value)
        self.assertEqual(get_next_action(WorkflowState.VERIFIED, phase=2), "learning_record")

    def test_retry_flow(self) -> None:
        wid = "WF-RETRY-001"
        workflow_manager.create(wid, db_path=self.db_path)
        workflow_manager.transition(wid, WorkflowState.FAILED.value, db_path=self.db_path)
        workflow_manager.transition(wid, WorkflowState.RETRY_PENDING.value, db_path=self.db_path)
        workflow_manager.transition(wid, WorkflowState.PUBLISHING.value, db_path=self.db_path)
        row = workflow_manager.load(wid, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.PUBLISHING.value)

    def test_pending_approval_cannot_skip_to_approved_without_approve(self) -> None:
        self.assertFalse(can_transition(WorkflowState.PENDING_APPROVAL, WorkflowState.PUBLISHING))

    def test_edit_requested_next_action_is_content_in_phase1(self) -> None:
        self.assertEqual(get_next_action(WorkflowState.EDIT_REQUESTED), "content")


if __name__ == "__main__":
    unittest.main()
