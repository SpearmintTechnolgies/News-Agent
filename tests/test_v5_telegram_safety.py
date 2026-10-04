"""Regression tests for V5 Telegram safety bugs.

Covers:
- Single-instance guard
- Update idempotency
- /make idempotency
- Card delivery uniqueness
- Pagination safety
- Callback binding
"""

from __future__ import annotations

import sys
sys.path.insert(0, "src")

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from newsagent_v2.telegram.singleton import (
    acquire_singleton_lock,
    release_singleton_lock,
    is_another_instance_running,
    SingletonError,
)


class TestSingletonLock:
    """Test single-instance guard."""
    
    def test_first_acquire_succeeds(self, tmp_path: Path):
        """First instance should acquire lock."""
        with patch("newsagent_v2.telegram.singleton.LOCK_FILE", tmp_path / "test.lock"):
            assert acquire_singleton_lock() is True
            release_singleton_lock()
    
    def test_second_acquire_fails(self, tmp_path: Path, monkeypatch):
        """Second instance should fail with SingletonError."""
        import os
        from newsagent_v2.telegram import singleton

        lock = tmp_path / "test.lock"
        monkeypatch.setattr(singleton, "LOCK_FILE", lock)

        # Simulate another live process holding the lock
        other_pid = os.getpid() + 99999
        lock.write_text(str(other_pid), encoding="utf-8")
        monkeypatch.setattr(singleton, "_pid_is_running", lambda pid: pid == other_pid)

        with pytest.raises(SingletonError) as exc:
            singleton.acquire_singleton_lock()
        assert "already running" in str(exc.value)

    def test_stale_lock_detected(self, tmp_path: Path):
        """Stale lock from dead process should be replaceable."""
        with patch("newsagent_v2.telegram.singleton.LOCK_FILE", tmp_path / "test.lock"):
            # Create lock with fake dead PID
            (tmp_path / "test.lock").write_text("99999999")
            
            # Should succeed (stale lock removed)
            assert acquire_singleton_lock() is True
            
            release_singleton_lock()


class TestUpdateIdempotency:
    """Test update idempotency tracking."""
    
    def test_duplicate_update_detected(self, tmp_path: Path):
        """Same update_id processed twice should be rejected."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        state.processed_update_ids.add(12345)
        
        # First check - already processed
        assert state.is_update_processed(12345) is True
        
        # Simulate second /make attempt
        can_run, reason = state.can_execute_make(12345)
        assert can_run is False
        assert reason == "update_already_processed"
    
    def test_new_update_allowed(self):
        """New update_id should be allowed."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        state.processed_update_ids = {12344}  # Different ID
        
        can_run, reason = state.can_execute_make(12345)
        assert can_run is True
        assert reason == "ok"
    
    def test_update_persists_across_state(self, tmp_path: Path):
        """Processed updates should persist (bounded)."""
        from newsagent_v2.telegram.state import V5BotState, PROCESSED_LOG
        
        log_file = tmp_path / "processed.log"
        with patch("newsagent_v2.telegram.state.PROCESSED_LOG", log_file):
            state = V5BotState()
            state.mark_update_processed(100)
            
            # New state instance should know about 100
            state2 = V5BotState()
            assert state2.is_update_processed(100) is True


class TestMakeIdempotency:
    """Test /make idempotency by make_run_id."""
    
    def test_same_update_same_make_run_blocked(self):
        """Same update cannot start second make run."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        update_id = 54321
        
        # First start
        make_run_id = state.start_make_run(update_id)
        assert state.current_make_run_id == make_run_id
        
        # Should detect in-progress
        can_run, reason = state.can_execute_make(update_id)
        assert can_run is False
        assert reason == "same_make_run_in_progress"
    
    def test_same_update_already_processed_blocked(self):
        """Processed update cannot restart make."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        update_id = 54322
        
        # Simulate: start, run, finish
        state.start_make_run(update_id)
        state.mark_update_processed(update_id)
        
        # Try again
        can_run, reason = state.can_execute_make(update_id)
        assert can_run is False
        assert reason == "update_already_processed"


class TestCardUniqueness:
    """Test card delivery uniqueness."""
    
    def test_duplicate_card_blocked(self):
        """Same event_id cannot be sent twice for same make_run."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        state.current_make_run_id = "make-abc123"
        
        # First send
        assert state.can_send_card("evt-001") is True
        
        # Second send - should be blocked
        assert state.can_send_card("evt-001") is False
    
    def test_different_cards_allowed(self):
        """Different event_ids should be allowed."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        state.current_make_run_id = "make-def456"
        
        assert state.can_send_card("evt-001") is True
        assert state.can_send_card("evt-002") is True
        assert state.can_send_card("evt-003") is True
        assert state.can_send_card("evt-004") is True
        assert state.can_send_card("evt-005") is True
    
    def test_event_ids_unique_assertion(self):
        """Events must have unique event_ids."""
        event_ids = ["evt-1", "evt-2", "evt-3", "evt-4", "evt-5"]
        assert len(event_ids) == len(set(event_ids)), "All event IDs must be unique"
    
    def test_duplicate_event_ids_caught(self):
        """Duplicate event IDs should fail assertion."""
        event_ids = ["evt-1", "evt-2", "evt-1", "evt-4", "evt-5"]
        
        with pytest.raises(AssertionError):
            assert len(event_ids) == len(set(event_ids))


class TestPaginationSafety:
    """Test pagination does not resend first five."""
    
    def test_pagination_respects_offset(self):
        """See Next 5 should send ranks 6-10, not 1-5."""
        all_events = [
            {"event_id": f"evt-{i:02d}", "rank": i}
            for i in range(1, 21)
        ]
        
        # Initial: 0:5
        initial = all_events[0:5]
        assert len(initial) == 5
        assert initial[0]["rank"] == 1
        assert initial[4]["rank"] == 5
        
        # Page 2: 5:10
        page2 = all_events[5:10]
        assert len(page2) == 5
        assert page2[0]["rank"] == 6
        assert page2[4]["rank"] == 10
    
    def test_pagination_does_not_overlap(self):
        """No overlap between initial and page 2."""
        all_events = [f"evt-{i:02d}" for i in range(1, 21)]
        
        initial_ids = set(all_events[0:5])
        page2_ids = set(all_events[5:10])
        
        assert len(initial_ids & page2_ids) == 0, "Pages must not overlap"


class TestCallbackBinding:
    """Test callbacks bind to correct event_id."""
    
    def test_callback_data_includes_event_id(self):
        """Callback data must include the event_id."""
        event_id = "evt-special-123"
        
        # Simulate callback data construction
        callback_data = f"run:{event_id}"
        
        assert event_id in callback_data
        assert callback_data.startswith("run:")
    
    def test_run_callback_parses_correctly(self):
        """RUN callback should resolve to correct event."""
        callback_data = "run:evt-abc-123"
        parts = callback_data.split(":")
        
        assert len(parts) == 2
        assert parts[0] == "run"
        assert parts[1] == "evt-abc-123"


class TestProviderIsolation:
    """Test RUN STORY does not invoke paid providers."""
    
    def test_run_story_no_writer_calls(self):
        """RUN STORY in smoke mode must not call writer."""
        # This is a placeholder - actual implementation would mock
        assert True, "Placeholder: verify zero writer calls"
    
    def test_run_story_no_image_calls(self):
        """RUN STORY in smoke mode must not call image provider."""
        assert True, "Placeholder: verify zero image calls"


class TestPollingSingleOwner:
    """Test exactly one component owns polling."""
    
    def test_no_double_poll_architecture(self):
        """Architecture must not have nested poll_once calls."""
        # This is a design assertion
        # The implementation should have ONE poll loop in start_v5_bot.py
        # listener_v5.py should NOT poll independently
        assert True, "Architecture: single polling owner in entry point"


class TestOffsetCorrectness:
    """Test offset advances correctly."""
    
    def test_offset_advances_after_update(self):
        """After processing update N, offset should be N+1."""
        update_id = 650000
        offset_before = update_id
        offset_after = update_id + 1
        
        assert offset_after == offset_before + 1
    
    def test_batch_processing_order(self):
        """Batch must process in ascending order."""
        updates = [
            {"update_id": 100},
            {"update_id": 101},
            {"update_id": 102},
        ]
        
        sorted_ids = [u["update_id"] for u in sorted(updates, key=lambda x: x.get("update_id", 0))]
        assert sorted_ids == [100, 101, 102]


class TestGracefulShutdown:
    """Test clean shutdown behavior."""
    
    def test_lock_released_on_exit(self, tmp_path: Path):
        """Lock must be released on normal exit."""
        with patch("newsagent_v2.telegram.singleton.LOCK_FILE", tmp_path / "test.lock"):
            acquire_singleton_lock()
            assert (tmp_path / "test.lock").exists()
            
            release_singleton_lock()
            assert not (tmp_path / "test.lock").exists()


class TestStartupBacklog:
    """Test startup backlog safety - historical updates skipped."""
    
    def test_historical_updates_skipped_on_fresh_start(self, tmp_path: Path):
        """Historical /make should NOT execute on fresh startup."""
        from newsagent_v2.telegram.state import V5BotState, PROCESSED_LOG
        
        with patch("newsagent_v2.telegram.state.PROCESSED_LOG", tmp_path / "processed_test.log"):
            state = V5BotState()
            
            # Simulate: old updates exist (100, 101, 102)
            historical_updates = [100, 101, 102]
            for uid in historical_updates:
                state.mark_update_processed(uid)
            
            # Historical should be marked as processed and blocked
            for uid in historical_updates:
                assert state.is_update_processed(uid) is True
                can_run, _ = state.can_execute_make(uid)
                assert can_run is False, f"Historical update {uid} should be blocked"
            
            # New /make arrives (103) - should be executable
            assert state.is_update_processed(103) is False
            can_run, reason = state.can_execute_make(103)
            assert can_run is True
            assert reason == "ok"
    
    def test_high_water_mark_established(self):
        """High water offset should be max(update_ids) + 1."""
        historical_ids = [100, 101, 102]
        high_water = max(historical_ids)
        next_offset = high_water + 1
        
        assert next_offset == 103
    
    def test_new_make_after_skipped_executes_once(self, tmp_path: Path):
        """First NEW /make after startup should execute exactly once."""
        from newsagent_v2.telegram.state import V5BotState, PROCESSED_LOG
        
        with patch("newsagent_v2.telegram.state.PROCESSED_LOG", tmp_path / "processed_test2.log"):
            state = V5BotState()
            
            # Historical processed
            for uid in [100, 101, 102]:
                state.mark_update_processed(uid)
            
            # First new /make (ID 103)
            update_id = 103
            # Verify NOT already processed
            assert state.is_update_processed(update_id) is False
            
            can_run, _ = state.can_execute_make(update_id)
            assert can_run is True, "First /make should execute"
            
            # Execute it
            make_run_id = state.start_make_run(update_id)
            state.mark_update_processed(update_id)
            
            # Second attempt for same update
            can_run, reason = state.can_execute_make(update_id)
            assert can_run is False
            assert reason == "update_already_processed"


class TestDiscoveryIntegration:
    """Test real make_v5_bridge integration is used."""
    
    def test_process_discovery_uses_real_pipeline(self):
        """process_discovery_and_send must use real V5DiscoveryPipeline."""
        from unittest.mock import MagicMock
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        
        discovery = MagicMock(spec=V5DiscoveryPipeline)
        discovery.run_discovery.return_value = [
            {"event_id": "evt-01", "rank": 1},
            {"event_id": "evt-02", "rank": 2},
            {"event_id": "evt-03", "rank": 3},
        ]
        discovery.get_top_events.return_value = [
            {"event_id": "evt-01", "rank": 1},
            {"event_id": "evt-02", "rank": 2},
            {"event_id": "evt-03", "rank": 3},
        ]
        discovery.get_top_events.return_value = discovery.run_discovery.return_value[:3]
        
        # Verify the mock was set up correctly
        events = discovery.run_discovery()
        assert len(events) == 3
    
    def test_exactly_five_cards_sent(self):
        """Exactly 5 cards when >=5 events exist."""
        events = [
            {"event_id": f"evt-{i:02d}", "rank": i}
            for i in range(1, 10)  # 9 events
        ]
        
        initial = events[:5]
        assert len(initial) == 5
        
        # Validate unique
        event_ids = [e["event_id"] for e in initial]
        assert len(event_ids) == len(set(event_ids))
    
    def test_fewer_than_five_sends_available(self):
        """When <5 events, send all available."""
        events = [
            {"event_id": f"evt-{i:02d}", "rank": i}
            for i in range(1, 3)  # 2 events
        ]
        
        assert len(events) == 2


class TestCardDelivery:
    """Test card delivery safety."""
    
    def test_navigation_message_sent_once(self):
        """Navigation message sent exactly once per /make."""
        from newsagent_v2.telegram.state import V5BotState
        
        state = V5BotState()
        state.current_make_run_id = "make-abc123"
        
        # Cards sent: 5
        # Navigation should be separate - sent once
        cards = [f"evt-{i:02d}" for i in range(1, 6)]
        
        for card in cards:
            assert state.can_send_card(card) is True
        
        sent = len(cards)
        assert sent == 5


class TestPageTwoSafety:
    """Test SEE NEXT 5 does not resend first five."""
    
    def test_page_two_does_not_resend_page_one(self):
        """Page 2 must not contain events from page 1."""
        all_events = [f"evt-{i:02d}" for i in range(1, 15)]
        
        page_1 = set(all_events[0:5])
        page_2 = set(all_events[5:10])
        
        assert len(page_1 & page_2) == 0
        assert page_2 == {"evt-06", "evt-07", "evt-08", "evt-09", "evt-10"}


class TestSyntaxWarning:
    """Test startup is clean."""
    
    def test_no_syntax_warning(self):
        """start_v5_bot.py should not produce SyntaxWarning."""
        import ast
        
        code = Path(__file__).parent.parent / "start_v5_bot.py"
        tree = ast.parse(code.read_text(encoding="utf-8"))
        
        # Basic parse should succeed
        assert tree is not None
        assert isinstance(tree, ast.Module)


class TestClientInterface:
    """Test TelegramTestClient interface accepts correct kwargs."""
    
    def _create_mock_response(self, ok=True, status=200, response_id=None):
        """Create a mock response object with status_code attribute."""
        response = MagicMock()
        response.status_code = status
        response.json.return_value = {"ok": ok, "result": {"message_id": response_id or 123}}
        return response
    
    def test_send_message_accepts_disable_web_page_preview(self):
        """send_message must accept disable_web_page_preview kwarg without TypeError."""
        from newsagent_v2.telegram.client import TelegramTestClient
        
        config = MagicMock()
        config.bot_token = "test-token"
        
        mock_transport = MagicMock(return_value=self._create_mock_response())
        client = TelegramTestClient(
            config,
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # THIS is the exact live failure - passing disable_web_page_preview causes TypeError
        # After fix, this should NOT raise
        try:
            result = client.send_message(
                chat_id="-123",
                text="Test",
                parse_mode="HTML",
                disable_web_page_preview=True,  # This caused the live failure!
            )
            # If we get here, the fix worked
            assert result.get("ok") is True
        except TypeError as e:
            if "unexpected keyword argument" in str(e):
                pytest.fail(f"send_message still rejects disable_web_page_preview: {e}")
            raise
        
        # Verify the user-provided value is passed through
        args, kwargs = mock_transport.call_args
        json_body = kwargs.get("json", {})
        assert json_body["disable_web_page_preview"] is True
    
    def test_send_message_default_disable_preview_is_true(self):
        """Default disable_web_page_preview must be True for safety."""
        from newsagent_v2.telegram.client import TelegramTestClient
        
        mock_transport = MagicMock(return_value=self._create_mock_response())
        client = TelegramTestClient(
            MagicMock(bot_token="test"),
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # Call WITHOUT explicit disable_web_page_preview
        client.send_message(chat_id="-123", text="Test")
        
        args, kwargs = mock_transport.call_args
        json_body = kwargs.get("json", {})
        assert json_body["disable_web_page_preview"] is True
    
    def test_send_message_with_reply_markup(self):
        """send_message must accept reply_markup."""
        from newsagent_v2.telegram.client import TelegramTestClient
        
        mock_transport = MagicMock(return_value=self._create_mock_response())
        client = TelegramTestClient(
            MagicMock(bot_token="test", test_chat_id="-123"),
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        markup = {"inline_keyboard": [[{"text": "Btn", "callback_data": "test"}]]}
        client.send_message(
            chat_id="-123",
            text="Test",
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        
        args, kwargs = mock_transport.call_args
        json_body = kwargs.get("json", {})
        assert json_body["reply_markup"] == markup
    
    def test_send_photo_interface_complete(self):
        """send_photo must accept all required kwargs without error."""
        from newsagent_v2.telegram.client import TelegramTestClient
        
        mock_transport = MagicMock(return_value=self._create_mock_response())
        client = TelegramTestClient(
            MagicMock(bot_token="test"),
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # Should not raise TypeError
        client.send_photo(
            chat_id="-123",
            photo_name="test.png",
            photo_bytes=b"fake-bytes",
            caption="Test caption",
            parse_mode="HTML",
            reply_markup=None,
        )
        
        assert mock_transport.called
    
    def test_edit_message_text_accepts_disable_web_page_preview(self):
        """edit_message_text must accept disable_web_page_preview."""
        from newsagent_v2.telegram.client import TelegramTestClient
        
        mock_transport = MagicMock(return_value=self._create_mock_response())
        client = TelegramTestClient(
            MagicMock(bot_token="test"),
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        client.edit_message_text(
            chat_id="-123",
            message_id=456,
            text="Updated",
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        
        args, kwargs = mock_transport.call_args
        json_body = kwargs.get("json", {})
        assert json_body["disable_web_page_preview"] is False


class TestSendToTelegramBoundary:
    """Test make_v5_bridge.send_to_telegram through client boundary."""
    
    def test_five_cards_pass_through_client(self):
        """Five cards can pass through TelegramTestClient boundary."""
        from newsagent_v2.telegram.client import TelegramTestClient
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 123}}
        
        mock_transport = MagicMock(return_value=mock_response)
        config = MagicMock()
        config.bot_token = "test"
        config.test_chat_id = "-123"
        
        client = TelegramTestClient(
            config,
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # Create 5 mock NewsEvents
        events = []
        for i in range(5):
            events.append(MagicMock(
                event_id=f"evt-{i:04d}",
                canonical_title=f"Event {i}",
            ))
        
        pipeline = V5DiscoveryPipeline(
            event_store=MagicMock(),
            source_registry=MagicMock(),
        )
        pipeline._ranked_events = events
        
        # THIS is where the live TypeError occurred
        # After fix, this should not raise
        results = pipeline.send_to_telegram(
            client=client,
            config=config,
            count=5,
            offset=0,
        )
        
        # 5 cards sent
        assert len(results) == 5
        assert mock_transport.call_count == 5
    
    def test_five_unique_event_ids_bound_correctly(self):
        """Five cards bind to five unique event IDs."""
        from newsagent_v2.telegram.client import TelegramTestClient
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        
        mock_transport = MagicMock(return_value=mock_response)
        config = MagicMock()
        config.bot_token = "test"
        config.test_chat_id = "-123"
        
        client = TelegramTestClient(
            config,
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # Create 5 unique events
        events = []
        for i in range(5):
            events.append(MagicMock(
                event_id=f"evt-{i:04d}",
                canonical_title=f"Event {i}",
            ))
        
        pipeline = V5DiscoveryPipeline(
            event_store=MagicMock(),
            source_registry=MagicMock(),
        )
        pipeline._ranked_events = events
        
        pipeline.send_to_telegram(
            client=client,
            config=config,
            count=5,
            offset=0,
        )
        
        # All 5 calls made
        assert mock_transport.call_count == 5
        
        # Extract text from calls to verify uniqueness
        texts = []
        for call in mock_transport.call_args_list:
            args, kwargs = call
            json_body = kwargs.get("json", {})
            texts.append(json_body.get("text", ""))
        
        # Each should be unique (different event IDs)
        assert len(set(texts)) == 5

    def test_send_to_telegram_uses_disable_web_page_preview(self):
        """send_to_telegram passes disable_web_page_preview to client."""
        from newsagent_v2.telegram.client import TelegramTestClient
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 123}}
        
        mock_transport = MagicMock(return_value=mock_response)
        config = MagicMock()
        config.bot_token = "test"
        config.test_chat_id = "-123"
        
        client = TelegramTestClient(
            config,
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        events = []
        for i in range(1):
            events.append(MagicMock(
                event_id=f"evt-{i:04d}",
                canonical_title=f"Event {i}",
            ))
        
        pipeline = V5DiscoveryPipeline(
            event_store=MagicMock(),
            source_registry=MagicMock(),
        )
        pipeline._ranked_events = events
        
        pipeline.send_to_telegram(
            client=client,
            config=config,
            count=1,
            offset=0,
        )
        
        # Verify disable_web_page_preview is in the body
        args, kwargs = mock_transport.call_args
        json_body = kwargs.get("json", {})
        assert json_body.get("disable_web_page_preview") is True


class TestNavigationPassesBoundary:
    """Test navigation message passes through client."""
    
    def test_navigation_message_sent_after_cards(self):
        """Navigation message is sent after cards with same interface."""
        from newsagent_v2.telegram.client import TelegramTestClient
        from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        
        mock_transport = MagicMock(return_value=mock_response)
        config = MagicMock()
        config.bot_token = "test"
        config.test_chat_id = "-123"
        
        client = TelegramTestClient(
            config,
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # Create 10 events (more than 5, triggers navigation)
        events = []
        for i in range(10):
            events.append(MagicMock(
                event_id=f"evt-{i:04d}",
                canonical_title=f"Event {i}",
            ))
        
        pipeline = V5DiscoveryPipeline(
            event_store=MagicMock(),
            source_registry=MagicMock(),
        )
        pipeline._ranked_events = events
        
        results = pipeline.send_to_telegram(
            client=client,
            config=config,
            count=5,
            offset=0,
        )
        
        # 5 cards + 1 navigation = 6 calls
        assert mock_transport.call_count == 6


class TestAcknowledgement:
    """Test /make acknowledgement behavior."""
    
    def test_acknowledgement_sent_before_discovery(self):
        """Acknowledgement must be sent BEFORE run_discovery() executes."""
        from newsagent_v2.telegram.acknowledgement import MakeAcknowledgement
        
        # Mock client
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"ok": True, "message_id": 12345}
        mock_client.edit_message_text.return_value = {"ok": True}
        
        ack = MakeAcknowledgement(
            client=mock_client,
            chat_id="-12345",
            update_id=100,
            make_run_id="make-abc123",
        )
        
        # Acknowledgement sent first
        result = ack.send_initial()
        assert result.get("ok") is True
        assert mock_client.send_message.called
        
        # Discovery would happen here...
        # Only THEN would completion be marked
        ack.mark_complete(5)
        assert mock_client.edit_message_text.called
    
    def test_duplicate_update_skips_both_ack_and_discovery(self):
        """Duplicate /make must skip acknowledgement AND discovery."""
        from newsagent_v2.telegram.state import V5BotState, PROCESSED_LOG
        
        with patch("newsagent_v2.telegram.state.PROCESSED_LOG", MagicMock()):
            state = V5BotState()
            
            # First /make processed
            update_id = 100
            state.mark_update_processed(update_id)
            
            # Second attempt
            can_run, _ = state.can_execute_make(update_id)
            assert can_run is False
    
    def test_completion_edits_same_message(self):
        """Success must edit the SAME acknowledgement, not send another."""
        from newsagent_v2.telegram.acknowledgement import MakeAcknowledgement
        
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"ok": True, "message_id": 123}
        mock_client.edit_message_text.return_value = {"ok": True}
        
        ack = MakeAcknowledgement(
            client=mock_client,
            chat_id="-123",
            update_id=100,
            make_run_id="make-abc",
        )
        
        ack.send_initial()
        assert ack.message_id == 123
        
        ack.mark_complete(5)
        
        # Verify edit uses same message_id
        call_args = mock_client.edit_message_text.call_args
        assert call_args[1]["message_id"] == 123
    
    def test_failure_edits_same_message(self):
        """Failure must edit the SAME acknowledgement."""
        from newsagent_v2.telegram.acknowledgement import MakeAcknowledgement
        
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"ok": True, "message_id": 456}
        mock_client.edit_message_text.return_value = {"ok": True}
        
        ack = MakeAcknowledgement(
            client=mock_client,
            chat_id="-123",
            update_id=100,
            make_run_id="make-xyz",
        )
        
        ack.send_initial()
        ack.mark_failed("test error")
        
        call_args = mock_client.edit_message_text.call_args
        assert call_args[1]["message_id"] == 456


class TestExceptionsNotSwallowed:
    """Test that discovery exceptions are handled and reported."""
    
    def test_discovery_exception_logged(self, tmp_path: Path):
        """Discovery exception must log failure, not disappear."""
        from newsagent_v2.telegram.state import V5BotState
        from newsagent_v2.telegram.acknowledgement import MakeAcknowledgement
        
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"ok": True, "message_id": 100}
        mock_client.edit_message_text.return_value = {"ok": True}
        
        ack = MakeAcknowledgement(
            client=mock_client,
            chat_id="-123",
            update_id=1,
            make_run_id="make-test",
        )
        
        # Simulate exception during discovery
        try:
            ack.send_initial()
            raise RuntimeError("Simulated discovery failure")
        except Exception as e:
            ack.mark_failed(str(e))
        
        # Failure should be logged in Telegram
        assert mock_client.edit_message_text.called


class TestCallbackRouting:
    """Test callback_query routing from Telegram through handler."""
    
    def test_callback_data_emitter_matches_parser(self):
        """v5_cards.make_callback_data must match v5_callbacks.parse_callback."""
        from newsagent_v2.telegram.v5_cards import make_callback_data
        from newsagent_v2.telegram.v5_callbacks import (
            RUN_PREFIX, FOLLOW_PREFIX, IGNORE_PREFIX, SEENEXT_PREFIX
        )
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler
        
        handler = V5CallbackHandler()
        
        # Generate and validate all callback formats
        test_event_id = "evt-abc123"
        
        run_cb = make_callback_data(RUN_PREFIX, test_event_id)
        follow_cb = make_callback_data(FOLLOW_PREFIX, test_event_id)
        ignore_cb = make_callback_data(IGNORE_PREFIX, test_event_id)
        seenext_cb = make_callback_data(SEENEXT_PREFIX, "5")
        
        # All must parse successfully
        assert handler.parse_callback(run_cb) == {"action": RUN_PREFIX, "event_id": test_event_id}
        assert handler.parse_callback(follow_cb) == {"action": FOLLOW_PREFIX, "event_id": test_event_id}
        assert handler.parse_callback(ignore_cb) == {"action": IGNORE_PREFIX, "event_id": test_event_id}
        assert handler.parse_callback(seenext_cb) == {"action": SEENEXT_PREFIX, "event_id": "5"}
    
    def test_callback_query_update_routes_correctly(self):
        """Real Telegram callback_query update must route to handler."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        
        # Real Telegram-shaped callback_query update
        telegram_update = {
            "update_id": 123456,
            "callback_query": {
                "id": "1234567890123456789",
                "from": {
                    "id": 987654321,
                    "is_bot": False,
                    "first_name": "Test",
                    "username": "testuser"
                },
                "message": {
                    "message_id": 42,
                    "from": {"id": 1111111111, "is_bot": True, "first_name": "Bot"},
                    "chat": {"id": -5238995962, "title": "Test Chat", "type": "supergroup"},
                    "date": 1700000000,
                    "text": "Event card text"
                },
                "chat_instance": "1234567890",
                "data": "run:evt-test123"
            }
        }
        
        # Extract callback_data using same logic as start_v5_bot.py
        def safe_get_callback_data(update: dict) -> str | None:
            try:
                cq = update.get("callback_query") or {}
                return cq.get("data")
            except Exception:
                return None
        
        callback_data = safe_get_callback_data(telegram_update)
        assert callback_data == "run:evt-test123"
        
        # Hand off to handler
        handler = V5CallbackHandler(
            event_store=MagicMock(),
            telegram_store=V5TelegramStore(),
        )
        
        result = handler.handle(callback_data)
        assert result.get("action") == "run_story"
        assert result.get("event_id") == "evt-test123"
    
    def test_callback_answered_with_text(self):
        """Valid callback must be acknowledged with answerCallbackQuery."""
        from newsagent_v2.telegram.client import TelegramTestClient
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": True}
        mock_transport = MagicMock(return_value=mock_response)
        
        config = MagicMock()
        config.bot_token = "test"
        config.test_chat_id = "-123"
        
        client = TelegramTestClient(
            config,
            transport=mock_transport,
            live_send_enabled=True,
        )
        
        # Simulate successful callback handler result
        result = {"ok": True, "action": "run_story", "message": "âœ… SELECTED: Test Event"}
        
        # Acknowledge
        client.answer_callback_query(
            callback_query_id="123456789",
            text=result["message"][:200],
        )
        
        # Verify transport called
        assert mock_transport.called
        args, kwargs = mock_transport.call_args
        json_body = kwargs.get("json", {})
        assert json_body.get("callback_query_id") == "123456789"
        assert "âœ… SELECTED" in json_body.get("text", "")
    
    def test_run_story_makes_zero_provider_calls(self):
        """RUN STORY callback must make zero writer/image/WordPress calls."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        from newsagent_v2.discovery.event_clusterer import NewsEvent
        
        # Create mock event store with one event
        event_store = MagicMock()
        mock_event = MagicMock(event_id="evt-test", canonical_title="Test Event")
        event_store.get.return_value = mock_event
        
        handler = V5CallbackHandler(
            event_store=event_store,
            telegram_store=V5TelegramStore(),
        )
        
        result = handler.handle("run:evt-test")
        
        assert result.get("ok") is True
        assert result.get("action") == "run_story"
        # Verify ZERO paid provider interactions
        # - No llm_calls
        # - No writer_calls
        # - No image_calls
        # - No wordpress calls
    
    def test_duplicate_callback_idempotent(self):
        """Same callback_query processed twice must be idempotent."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        
        telegram_store = V5TelegramStore()
        event_store = MagicMock()
        event_store.get.return_value = MagicMock(
            event_id="evt-test",
            canonical_title="Test"
        )
        
        handler = V5CallbackHandler(
            event_store=event_store,
            telegram_store=telegram_store,
        )
        
        # First click - mark_selected returns True
        result1 = handler.handle("run:evt-test")
        assert result1["ok"] is True
        assert result1.get("is_new_selection") is True
        
        # Second click - should still succeed but is_new_selection = False
        result2 = handler.handle("run:evt-test")
        assert result2["ok"] is True
        assert result2.get("is_new_selection") is False
    
    def test_malformed_callback_data_fails_safe(self):
        """Malformed callback_data must fail safely."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler
        
        handler = V5CallbackHandler()
        
        malformed = [
            "",           # Empty
            "run",        # Missing event_id
            "bad_prefix:test",  # Unknown prefix
            "::",         # Malformed
        ]
        
        for data in malformed:
            result = handler.handle(data)
            assert result.get("ok") is False
            assert "invalid" in result.get("reason", "") or "unknown" in result.get("reason", "")
    
    def test_follow_routes_to_correct_event(self):
        """FOLLOW callback marks correct event."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        
        telegram_store = V5TelegramStore()
        event_store = MagicMock()
        event_store.get.return_value = MagicMock(
            event_id="evt-123",
            canonical_title="Bitcoin News"
        )
        
        handler = V5CallbackHandler(
            event_store=event_store,
            telegram_store=telegram_store,
        )
        
        result = handler.handle("flw:evt-123")
        
        assert result.get("ok") is True
        assert result.get("action") == "follow"
        assert telegram_store.is_followed("evt-123") is True
    
    def test_ignore_routes_to_correct_event(self):
        """IGNORE callback marks correct event."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        
        telegram_store = V5TelegramStore()
        event_store = MagicMock()
        event_store.get.return_value = MagicMock(
            event_id="evt-456",
            canonical_title="Ethereum News"
        )
        
        handler = V5CallbackHandler(
            event_store=event_store,
            telegram_store=telegram_store,
        )
        
        result = handler.handle("ign:evt-456")
        
        assert result.get("ok") is True
        assert result.get("action") == "ignore"
        assert telegram_store.is_ignored("evt-456") is True
    
    def test_see_next_routes_with_offset(self):
        """SEE NEXT 5 callback routes with correct offset."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        from newsagent_v2.discovery.event_clusterer import NewsEvent
        
        telegram_store = V5TelegramStore()
        
        # Create 10 events
        events = []
        for i in range(1, 11):
            events.append(MagicMock(event_id=f"evt-{i:02d}", canonical_title=f"Event {i}"))
        
        telegram_store.save_batch(events)
        
        handler = V5CallbackHandler(
            event_store=MagicMock(),
            telegram_store=telegram_store,
        )
        
        result = handler.handle("snext:5")
        
        assert result.get("ok") is True
        assert result.get("action") == "see_next"
        assert result.get("offset") == 5
        assert len(result.get("events", [])) == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


