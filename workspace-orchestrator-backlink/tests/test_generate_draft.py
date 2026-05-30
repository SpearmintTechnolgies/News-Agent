#!/usr/bin/env python3
"""Tests for generate_draft skill."""
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
from generate_draft import build_draft_context, draft_workflow, generate_draft_text  # noqa: E402
from states import WorkflowState  # noqa: E402


class GenerateDraftTests(unittest.TestCase):
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
        self.opp, self.wf = backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            "https://example.com/write-for-us",
            "WF-DRAFT-001",
            title="Write For Us",
            db_path=self.db_path,
        )
        backlink_db.update_opportunity_fields(
            self.opp.id,
            placement_type="guest_post",
            final_score=72.0,
            context_json={"discovery_signals": {"guest_post_language": True, "has_form": True}},
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        self._tmpdir.cleanup()

    def test_guest_post_draft_includes_target_link(self) -> None:
        context = build_draft_context(self.wf.workflow_id, db_path=self.db_path)
        generated = generate_draft_text(context)
        self.assertIn("cryptography.com", generated.draft_text.lower())
        self.assertIn("Title:", generated.draft_text)
        self.assertEqual(generated.placement_type, "guest_post")

    def test_draft_workflow_saves_versioned_draft(self) -> None:
        generated, saved = draft_workflow(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(saved.version, 1)
        self.assertGreaterEqual(generated.confidence, 0.5)
        latest = backlink_db.get_latest_draft(self.wf.workflow_id, db_path=self.db_path)
        assert latest is not None
        self.assertEqual(latest.tone, generated.tone)

    def test_edit_prompt_adds_technical_revision(self) -> None:
        context = build_draft_context(self.wf.workflow_id, db_path=self.db_path)
        first = generate_draft_text(context)
        revised = generate_draft_text(
            context,
            edit_prompt="Make the tone more technical",
            previous_draft=first.draft_text,
        )
        self.assertIn("technical", revised.draft_text.lower())
        self.assertIn("Technical revision", revised.draft_text)

    def test_comment_placement_is_shorter(self) -> None:
        backlink_db.update_opportunity_fields(
            self.opp.id,
            placement_type="comment",
            db_path=self.db_path,
        )
        context = build_draft_context(self.wf.workflow_id, db_path=self.db_path)
        generated = generate_draft_text(context)
        self.assertLess(len(generated.draft_text), 800)
        self.assertNotIn("Title:", generated.draft_text)

    def test_driver_drafting_step(self) -> None:
        os.environ["BACKLINK_PHASE"] = "2"
        for state in (
            WorkflowState.DISCOVERED.value,
            WorkflowState.QUALIFIED.value,
            WorkflowState.PLACEMENT_CHECK.value,
        ):
            workflow_manager.transition(
                self.wf.workflow_id,
                state,
                agent="test-setup",
                db_path=self.db_path,
            )
        result = workflow_driver.run_step(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(result.action, "drafting")
        self.assertEqual(result.status, "ok")
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.DRAFTED.value)
        os.environ.pop("BACKLINK_PHASE", None)


if __name__ == "__main__":
    unittest.main()
