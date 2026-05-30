#!/usr/bin/env python3
"""Tests for score_candidate and detect_placement skills."""
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
from detect_placement import detect_placement  # noqa: E402
from score_candidate import score_candidate  # noqa: E402
from states import WorkflowState  # noqa: E402


class ScoreCandidateTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        os.environ.pop("BACKLINK_TELEGRAM_DRY_RUN", None)
        self._tmpdir.cleanup()

    def _opp(
        self,
        url: str,
        *,
        title: str | None = None,
        snippet: str | None = None,
        signals: dict | None = None,
    ) -> backlink_db.OpportunityRow:
        opp, _ = backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            url,
            workflow_manager.new_workflow_id(),
            title=title,
            snippet=snippet,
            db_path=self.db_path,
        )
        if signals:
            backlink_db.update_opportunity_fields(
                opp.id,
                context_json={"discovery_signals": signals},
                db_path=self.db_path,
            )
            refreshed = backlink_db.get_opportunity(opp.id, db_path=self.db_path)
            assert refreshed is not None
            return refreshed
        return opp

    def test_write_for_us_url_qualifies(self) -> None:
        opp = self._opp(
            "https://example.com/write-for-us",
            title="Write For Us",
            snippet="Submit your cryptography guest post",
        )
        score = score_candidate(opp, db_path=self.db_path)
        self.assertTrue(score.qualified)
        self.assertGreaterEqual(score.final_score, 50.0)

    def test_blacklisted_domain_disqualified(self) -> None:
        opp = self._opp(
            "https://spam.example.com/write-for-us",
            title="Write For Us",
        )
        backlink_db.add_blacklist(domain="spam.example.com", reason="test", db_path=self.db_path)
        score = score_candidate(opp, db_path=self.db_path)
        self.assertFalse(score.qualified)
        self.assertEqual(score.reason, "blacklisted")

    def test_low_relevance_disqualified(self) -> None:
        opp = self._opp("https://random.example.com/about", title="About Us")
        score = score_candidate(opp, db_path=self.db_path)
        self.assertFalse(score.qualified)
        self.assertEqual(score.reason, "below_threshold")

    def test_qualification_step_archives_low_score(self) -> None:
        _, wf = backlink_db.create_opportunity_and_workflow(
            self.campaign_id,
            "https://random.example.com/about",
            "WF-LOW-SCORE",
            title="About Us",
            db_path=self.db_path,
        )
        workflow_driver.run_step(wf.workflow_id, db_path=self.db_path)  # discovery
        result = workflow_driver.run_step(wf.workflow_id, db_path=self.db_path)  # qualification
        self.assertEqual(result.action, "scoring")
        row = workflow_manager.load(wf.workflow_id, db_path=self.db_path)
        self.assertEqual(row.state, WorkflowState.ARCHIVED.value)


class DetectPlacementTests(unittest.TestCase):
    def test_guest_post_from_signals(self) -> None:
        result = detect_placement(
            {
                "guest_post_language": True,
                "has_textarea_form": True,
                "has_form": True,
            }
        )
        self.assertEqual(result.placement_type, "guest_post")
        self.assertTrue(result.placement_allowed)

    def test_url_hint_fallback(self) -> None:
        result = detect_placement(
            {},
            title="Write For Us",
            url="https://example.com/write-for-us",
        )
        self.assertEqual(result.placement_type, "guest_post")
        self.assertTrue(result.placement_allowed)

    def test_unknown_without_hints(self) -> None:
        result = detect_placement({}, title="About", url="https://example.com/about")
        self.assertEqual(result.placement_type, "unknown")
        self.assertFalse(result.placement_allowed)


if __name__ == "__main__":
    unittest.main()
