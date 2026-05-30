#!/usr/bin/env python3
"""Tests for backlink Telegram approval card and callbacks."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))
sys.path.insert(0, str(_ROOT / "tools" / "telegram"))

import backlink_db  # noqa: E402
import workflow_driver  # noqa: E402
import workflow_manager  # noqa: E402
from handle_backlink_callback import (  # noqa: E402
    apply_edit_instructions,
    handle_approve,
    handle_blacklist,
    handle_callback,
    handle_edit_request,
    handle_reject,
    parse_callback,
    resolve_workflow_id,
)
from send_backlink_card import (  # noqa: E402
    build_card_payload,
    build_inline_keyboard,
    send_approval_card,
)
from states import WorkflowState  # noqa: E402


class TelegramBacklinkTests(unittest.TestCase):
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
            "WF-TG-001",
            title="Write For Us",
            db_path=self.db_path,
        )
        workflow_driver.run_until_wait(self.wf.workflow_id, db_path=self.db_path)

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        os.environ.pop("BACKLINK_SKIP_IMAGE_GEN", None)
        self._tmpdir.cleanup()

    def test_parse_callback(self) -> None:
        self.assertEqual(parse_callback("bl_approve:WF-TG-001"), ("approve", "WF-TG-001"))
        self.assertIsNone(parse_callback("oc_r:run:5"))

    def test_build_card_payload(self) -> None:
        payload = build_card_payload(self.wf.workflow_id, db_path=self.db_path)
        self.assertIn("bl-WF-TG-001", payload["alert_id"])
        self.assertIn("Approve", str(payload["reply_markup"]))
        self.assertNotIn("Blacklist", str(payload["reply_markup"]))
        content = backlink_db.get_latest_content_asset(self.wf.workflow_id, db_path=self.db_path)
        self.assertIsNotNone(content)
        self.assertEqual(payload["content_version"], content.version)

    def test_send_approval_card_dry_run(self) -> None:
        sent = []
        result = send_approval_card(
            self.wf.workflow_id,
            db_path=self.db_path,
            dry_run=False,
            force=True,
            sender=lambda payload: sent.append(payload) or {
                "telegram_chat_id": "-100123",
                "telegram_message_id": 42,
            },
        )
        self.assertEqual(result["telegram_message_id"], 42)
        self.assertEqual(len(sent), 1)
        approval = backlink_db.lookup_pending_approval(self.wf.workflow_id, db_path=self.db_path)
        self.assertIsNotNone(approval)
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertIsNotNone(row.approval_expires_at)

    def test_resolve_workflow_from_message(self) -> None:
        send_approval_card(
            self.wf.workflow_id,
            db_path=self.db_path,
            dry_run=False,
            force=True,
            sender=lambda _payload: {
                "telegram_chat_id": "-100123",
                "telegram_message_id": 99,
            },
        )
        wid = resolve_workflow_id(
            chat_id="-100123",
            reply_to_message_id=99,
            db_path=self.db_path,
        )
        self.assertEqual(wid, self.wf.workflow_id)

    def test_approve_callback(self) -> None:
        result = handle_callback(
            f"bl_approve:{self.wf.workflow_id}",
            user_id="user-1",
            db_path=self.db_path,
        )
        self.assertEqual(result["state"], WorkflowState.APPROVED.value)
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.APPROVED.value)
        draft = backlink_db.get_latest_draft(self.wf.workflow_id, db_path=self.db_path)
        assert draft is not None
        self.assertEqual(draft.approved, 1)

    def test_reject_callback(self) -> None:
        handle_callback(
            f"bl_reject:{self.wf.workflow_id}",
            user_id="user-1",
            db_path=self.db_path,
        )
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)

    def test_blacklist_callback(self) -> None:
        handle_callback(
            f"bl_blacklist:{self.wf.workflow_id}",
            user_id="user-1",
            db_path=self.db_path,
        )
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)
        self.assertTrue(
            backlink_db.is_blacklisted("example.com", None, db_path=self.db_path)
        )

    def test_edit_loop(self) -> None:
        handle_callback(
            f"bl_edit:{self.wf.workflow_id}",
            user_id="user-1",
            db_path=self.db_path,
        )
        row = workflow_manager.load(self.wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.EDIT_REQUESTED.value)

        result = apply_edit_instructions(
            self.wf.workflow_id,
            "Make the tone more technical",
            user_id="user-1",
            db_path=self.db_path,
        )
        self.assertEqual(result["state"], WorkflowState.PENDING_APPROVAL.value)
        draft = backlink_db.get_latest_draft(self.wf.workflow_id, db_path=self.db_path)
        assert draft is not None
        self.assertEqual(draft.version, 2)
        self.assertIn("technical", draft.draft_text)
        self.assertIsNone(backlink_db.get_edit_session(self.wf.workflow_id, db_path=self.db_path))

    def test_keyboard_callback_data_under_64_bytes(self) -> None:
        keyboard = build_inline_keyboard("WF-20260529-114457")
        for row in keyboard["inline_keyboard"]:
            for button in row:
                data = button.get("callback_data", "")
                self.assertLessEqual(len(data.encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
