"""Tests for controlled E2E generation confirmation flow.

Verifies:
- RUN STORY -> zero provider calls (just shows GENERATE NOW/CANCEL)
- RUN STORY -> GENERATE NOW -> exactly one generation
- duplicate GENERATE NOW -> still exactly one generation
- RUN STORY -> CANCEL -> zero generation
- second story GENERATE NOW while first active -> blocked
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from newsagent_v2.discovery.event_clusterer import NewsEvent, EventReport
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.v5_generation.generation_worker import GenerationWorker
from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight


class TestControlledGenerationFlow:
    """Test GENERATE NOW / CANCEL confirmation flow."""

    @pytest.fixture
    def mock_event(self):
        """Create a mock event."""
        event = NewsEvent(
            event_id="test-event-001",
            canonical_title="Test Crypto News",
            topic="technology",
            entities=frozenset(["crypto", "test"]),
        )
        event.reports.append(
            EventReport(
                report_id="rpt-001",
                source="test",
                source_id="src-001",
                source_authority=0.8,
                headline="Test Headline",
                url="https://example.com/1",
                published_at="2024-01-01T00:00:00Z",
                retrieved_at="2024-01-01T00:00:00Z",
                description="Test",
                entities=["crypto"],
                raw_item_id="raw-001",
            )
        )
        return event

    @pytest.fixture
    def controlled_handler(self, monkeypatch):
        """Create a V5CallbackHandler in controlled E2E mode."""
        environ = {
            "NEWSAGENT_V5_CONTROLLED_E2E": "true",
            "GROQ_API_KEY": "fake_key",
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us",
            "GOOGLE_APPLICATION_CREDENTIALS": "/fake/path.json",
        }

        event_store = Mock()
        telegram_store = V5TelegramStore()

        handler = V5CallbackHandler(
            event_store=event_store,
            telegram_store=telegram_store,
            environ=environ,
        )

        # Mock generation worker
        handler.generation_worker = Mock(spec=GenerationWorker)
        handler.generation_worker.get_active_job_count.return_value = 0
        handler.generation_worker.get_job_for_event.return_value = None  # No existing job
        handler.generation_worker.request_generation.return_value = {
            "ok": True,
            "job_id": "job-001",
            "state": "RESERVED",
            "new": True,
        }

        # Mock preflight
        handler.preflight = Mock(spec=ProviderPreflight)
        writer_status = Mock()
        writer_status.status = "READY"
        image_status = Mock()
        image_status.status = "READY"
        handler.preflight.check_writer.return_value = writer_status
        handler.preflight.check_image.return_value = image_status

        return handler

    def test_run_story_in_controlled_mode_shows_confirmation(self, controlled_handler, mock_event):
        """RUN STORY in controlled mode should show GENERATE NOW/CANCEL, not trigger generation."""
        # Setup
        controlled_handler.event_store.get.return_value = mock_event

        # Execute
        result = controlled_handler.handle_run_story(mock_event.event_id)

        # Verify
        assert result["ok"] is True
        assert result["controlled_e2e"] is True
        assert result["awaiting_confirmation"] is True
        assert result["started"] is False
        assert "reply_markup" in result

        # Check keyboard has GENERATE NOW and CANCEL
        keyboard = result["reply_markup"]
        assert len(keyboard["inline_keyboard"][0]) == 2
        assert keyboard["inline_keyboard"][0][0]["text"] == "🚀 GENERATE NOW"
        assert keyboard["inline_keyboard"][0][1]["text"] == "❌ CANCEL"

        # ZERO provider calls
        controlled_handler.generation_worker.request_generation.assert_not_called()

    def test_generate_now_triggers_generation(self, controlled_handler, mock_event):
        """GENERATE NOW should trigger actual generation with preflight check."""
        # Setup
        controlled_handler.event_store.get.return_value = mock_event
        controlled_handler.telegram_store._selected_event_ids.add(mock_event.event_id)

        # Execute
        result = controlled_handler.handle_generate_now(mock_event.event_id)

        # Verify
        assert result["ok"] is True
        assert result["action"] == "generate_now"
        assert result["started"] is True

        # Preflight was checked
        controlled_handler.preflight.check_writer.assert_called_once()
        controlled_handler.preflight.check_image.assert_called_once()

        # Generation worker was called exactly once
        controlled_handler.generation_worker.request_generation.assert_called_once()
        call_args = controlled_handler.generation_worker.request_generation.call_args
        assert call_args[1]["event"].event_id == mock_event.event_id

    def test_duplicate_generate_now_does_not_duplicate(self, controlled_handler, mock_event):
        """Duplicate GENERATE NOW should return existing job, not create duplicate."""
        # Setup
        controlled_handler.event_store.get.return_value = mock_event
        controlled_handler.telegram_store._selected_event_ids.add(mock_event.event_id)

        # Create a mock job to return on second call
        from newsagent_v2.v5_generation.persistent_store import GenerationJob
        mock_job = Mock(spec=GenerationJob)
        mock_job.job_id = "job-001"
        mock_job.state = "RESERVED"
        mock_job.event_id = mock_event.event_id

        # Mock get_job_for_event: returns None first time, then returns job
        call_count = [0]
        def mock_get_job(event_id):
            call_count[0] += 1
            return mock_job if call_count[0] > 1 else None
        controlled_handler.generation_worker.get_job_for_event.side_effect = mock_get_job

        # Execute twice
        result1 = controlled_handler.handle_generate_now(mock_event.event_id)
        result2 = controlled_handler.handle_generate_now(mock_event.event_id)

        # Both should succeed but second should not be new
        assert result1["ok"] is True
        assert result1.get("new") is not False  # First is new
        assert result2["ok"] is True
        assert result2.get("new") is False  # Second is not new

        # Was called only once (idempotency via handler)
        assert controlled_handler.generation_worker.request_generation.call_count == 1
        assert result2["job_id"] == "job-001"

    def test_cancel_does_not_generate(self, controlled_handler, mock_event):
        """CANCEL should cleanup state and make zero provider calls."""
        # Setup
        controlled_handler.event_store.get.return_value = mock_event
        controlled_handler.telegram_store._selected_event_ids.add(mock_event.event_id)

        # Execute
        result = controlled_handler.handle_cancel_generation(mock_event.event_id)

        # Verify
        assert result["ok"] is True
        assert result["action"] == "cancel_generation"

        # Zero provider calls
        controlled_handler.generation_worker.request_generation.assert_not_called()

        # Event removed from selected
        assert mock_event.event_id not in controlled_handler.telegram_store._selected_event_ids

    def test_second_story_blocked_when_first_active(self, controlled_handler, mock_event):
        """Second GENERATE NOW should be blocked when one is already active."""
        # Setup
        mock_event_2 = NewsEvent(
            event_id="test-event-002",
            canonical_title="Second Test Story",
            topic="technology",
            entities=frozenset(["crypto"]),
        )
        mock_event_2.reports.append(
            EventReport(
                report_id="rpt-002",
                source="test",
                source_id="src-002",
                source_authority=0.8,
                headline="Test",
                url="https://example.com/2",
                published_at="2024-01-01T00:00:00Z",
                retrieved_at="2024-01-01T00:00:00Z",
                description="Test",
                entities=["crypto"],
                raw_item_id="raw-002",
            )
        )

        controlled_handler.event_store.get.side_effect = lambda eid: {
            mock_event.event_id: mock_event,
            mock_event_2.event_id: mock_event_2,
        }.get(eid)

        controlled_handler.telegram_store._selected_event_ids.add(mock_event.event_id)
        controlled_handler.telegram_store._selected_event_ids.add(mock_event_2.event_id)

        # Mock: first call returns 0 (no active), second call returns 1 (first now active)
        call_count = [0]
        def mock_active_count():
            call_count[0] += 1
            return 0 if call_count[0] == 1 else 1
        controlled_handler.generation_worker.get_active_job_count.side_effect = mock_active_count

        # Execute first (should work - no active jobs yet)
        result1 = controlled_handler.handle_generate_now(mock_event.event_id)

        # Execute second (should be blocked - first now active)
        result2 = controlled_handler.handle_generate_now(mock_event_2.event_id)

        # First should succeed
        assert result1["ok"] is True

        # Second should be blocked
        assert result2["ok"] is False
        assert result2["reason"] == "max_active_reached"
        assert "Another generation is currently active" in result2["message"]

        # Verify only ONE generation request was made
        assert controlled_handler.generation_worker.request_generation.call_count == 1

    def test_generate_now_not_selected_returns_error(self, controlled_handler, mock_event):
        """GENERATE NOW on non-selected event should return error."""
        # Setup - event NOT in selected
        controlled_handler.event_store.get.return_value = mock_event
        # selected set is empty

        # Execute
        result = controlled_handler.handle_generate_now(mock_event.event_id)

        # Verify
        assert result["ok"] is False
        assert result["reason"] == "event_not_selected"

    def test_generate_now_with_missing_writer_ready(self, controlled_handler, mock_event):
        """GENERATE NOW should fail preflight if writer not ready."""
        # Setup
        controlled_handler.event_store.get.return_value = mock_event
        controlled_handler.telegram_store._selected_event_ids.add(mock_event.event_id)

        # Mock writer not ready
        writer_status = Mock()
        writer_status.status = "MISSING_CREDENTIALS"
        controlled_handler.preflight.check_writer.return_value = writer_status

        # Execute
        result = controlled_handler.handle_generate_now(mock_event.event_id)

        # Verify
        assert result["ok"] is False
        assert result["reason"] == "writer_not_ready"

    def test_generate_now_with_missing_image_ready(self, controlled_handler, mock_event):
        """GENERATE NOW should fail preflight if image not ready."""
        # Setup
        controlled_handler.event_store.get.return_value = mock_event
        controlled_handler.telegram_store._selected_event_ids.add(mock_event.event_id)

        # Mock image not ready
        image_status = Mock()
        image_status.status = "MISSING_CONFIG"
        controlled_handler.preflight.check_image.return_value = image_status

        # Execute
        result = controlled_handler.handle_generate_now(mock_event.event_id)

        # Verify
        assert result["ok"] is False
        assert result["reason"] == "image_not_ready"


class TestControlledE2ECallbackRouting:
    """Test that callbacks route correctly in controlled mode."""

    def test_review_callback_parsed_by_dispatcher(self):
        """V5CallbackHandler.parse_callback should recognize generation callbacks."""
        handler = V5CallbackHandler(event_store=Mock())

        # Parse RUN
        parsed = handler.parse_callback("run:event-001")
        assert parsed["action"] == "run"
        assert parsed["event_id"] == "event-001"

        # Parse GENERATE NOW
        parsed = handler.parse_callback("gen:event-001")
        assert parsed["action"] == "gen"
        assert parsed["event_id"] == "event-001"

        # Parse CANCEL
        parsed = handler.parse_callback("cancel:event-001")
        assert parsed["action"] == "cancel"
        assert parsed["event_id"] == "event-001"

    def test_handle_dispatches_correctly(self):
        """V5CallbackHandler.handle should dispatch to correct handler."""
        handler = V5CallbackHandler(
            event_store=Mock(),
            environ={"NEWSAGENT_V5_CONTROLLED_E2E": "true"},
        )

        # Mock event store
        mock_event = Mock()
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test"
        handler.event_store.get.return_value = mock_event

        # Test RUN dispatches
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(handler, "handle_run_story", Mock(return_value={"ok": True}))
            result = handler.handle("run:evt-001")
            handler.handle_run_story.assert_called_once_with("evt-001")

        # Test GENERATE dispatches
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(handler, "handle_generate_now", Mock(return_value={"ok": True}))
            result = handler.handle("gen:evt-001")
            handler.handle_generate_now.assert_called_once_with("evt-001")

        # Test CANCEL dispatches
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(handler, "handle_cancel_generation", Mock(return_value={"ok": True}))
            result = handler.handle("cancel:evt-001")
            handler.handle_cancel_generation.assert_called_once_with("evt-001")


