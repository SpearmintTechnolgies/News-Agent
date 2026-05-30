#!/usr/bin/env python3
"""Tests for workflow step validators."""
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

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from states import WorkflowState  # noqa: E402


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test_backlink.db")
        backlink_db.init_db(self.db_path)
        self.cid = backlink_db.get_or_create_campaign("test", "cryptography.com", db_path=self.db_path)
        self.wid = workflow_manager.new_workflow_id()
        self.opp, self.wf = backlink_db.create_opportunity_and_workflow(
            self.cid,
            "https://example.com/write-for-us",
            self.wid,
            title="Write For Us",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _run_validator(self, script: str) -> tuple[int, str, str]:
        import subprocess

        path = _ROOT / "workflows" / "validators" / script
        result = subprocess.run(
            [sys.executable, str(path), "--workflow-id", self.wid, "--db", self.db_path],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def test_validate_discovery_fails_on_new(self) -> None:
        code, out, err = self._run_validator("validate_discovery.py")
        self.assertEqual(code, 1)
        self.assertIn("STEP_FAIL", err)

    def test_validate_discovery_ok_after_transition(self) -> None:
        workflow_manager.transition(self.wid, WorkflowState.DISCOVERED.value, db_path=self.db_path)
        code, out, err = self._run_validator("validate_discovery.py")
        self.assertEqual(code, 0)
        self.assertIn("STEP_OK", out)

    def test_validate_score_ok_after_scored(self) -> None:
        workflow_manager.transition(self.wid, WorkflowState.DISCOVERED.value, db_path=self.db_path)
        workflow_manager.transition(self.wid, WorkflowState.SCORED.value, db_path=self.db_path)
        backlink_db.update_opportunity_fields(self.opp.id, final_score=0.82, db_path=self.db_path)
        code, out, err = self._run_validator("validate_score.py")
        self.assertEqual(code, 0)
        self.assertIn("STEP_OK", out)

    def test_validate_audit_ok_with_row(self) -> None:
        workflow_manager.transition(self.wid, WorkflowState.DISCOVERED.value, db_path=self.db_path)
        workflow_manager.transition(self.wid, WorkflowState.SCORED.value, db_path=self.db_path)
        workflow_manager.transition(self.wid, WorkflowState.AUDITED.value, db_path=self.db_path)
        backlink_db.save_audit(
            self.wid,
            {"placement_type": "guest_post", "passed": True},
            opportunity_id=self.opp.id,
            placement_type="guest_post",
            passed=True,
            audit_score=0.9,
            db_path=self.db_path,
        )
        code, out, err = self._run_validator("validate_audit.py")
        self.assertEqual(code, 0)
        self.assertIn("STEP_OK", out)


if __name__ == "__main__":
    unittest.main()
