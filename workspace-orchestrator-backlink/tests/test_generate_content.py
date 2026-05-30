#!/usr/bin/env python3
"""Tests for generate_content image retry / fail-open behavior."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))
sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from generate_content import IMAGE_GEN_ATTEMPTS, generate_content  # noqa: E402
from states import WorkflowState  # noqa: E402


class GenerateContentImageTests(unittest.TestCase):
    def setUp(self) -> None:
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
            "WF-IMG-001",
            title="Write For Us",
            db_path=self.db_path,
        )
        backlink_db.update_opportunity_fields(
            self.opp.id,
            placement_type="guest_post",
            final_score=72.0,
            db_path=self.db_path,
        )
        for state in (
            WorkflowState.DISCOVERED.value,
            WorkflowState.SCORED.value,
            WorkflowState.AUDITED.value,
        ):
            workflow_manager.transition(
                self.wf.workflow_id,
                state,
                agent="test-setup",
                db_path=self.db_path,
            )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    @patch("generate_content._generate_feature_image", return_value=None)
    @patch("generate_content.time.sleep")
    def test_image_fail_after_retries_continues(
        self,
        _sleep: object,
        _gen: object,
    ) -> None:
        result, saved = generate_content(self.wf.workflow_id, db_path=self.db_path)
        self.assertIsNone(result.image_local_path)
        self.assertIsNotNone(result.image_error)
        self.assertIn("3 attempts", result.image_error)
        self.assertIsNone(saved.image_local_path)
        self.assertTrue(saved.image_url.startswith("failed:"))
        _gen.assert_called()
        self.assertEqual(_gen.call_count, IMAGE_GEN_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
