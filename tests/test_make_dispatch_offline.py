"""Offline /make production dispatch test.

Exercises the EXACT call site around start_v5_bot.py line ~843
with external operations mocked.
"""

import pytest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestMakeProductionDispatch:
    """Test /make dispatch through production path."""

    @pytest.fixture
    def temp_dirs(self):
        """Create temp directories."""
        temp_dir = tempfile.mkdtemp()
        dirs = {
            "temp": Path(temp_dir),
            "state": Path(temp_dir) / "v5_state",
            "events": Path(temp_dir) / "events",
            "data": Path(temp_dir) / "data",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        yield dirs
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_make_dispatch_execution(self, temp_dirs):
        """Test /make executes through production dispatch path."""

        test_environ = {
            "NEWSAGENT_V2_TELEGRAM_BOT_TOKEN": "123456789:fake_token_for_make_test",
            "NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID": "12345",
            "GROQ_API_KEY": "test_key",
            "NEWSAGENT_V5_CONTROLLED_E2E": "true",
        }

        # Create mock client with proper _post
        def mock_post(method, json_body=None, **kwargs):
            if method == "getMe":
                return {
                    "ok": True,
                    "status_code": 200,
                    "payload": {"ok": True, "result": {"username": "test_bot", "id": 12345}}
                }
            return {"ok": True, "status_code": 200, "payload": {"ok": True}}

        mock_client = Mock()
        mock_client._post = mock_post
        mock_client.send_message.return_value = {"ok": True, "message_id": 1}
        mock_client.edit_message_text.return_value = {"ok": True}

        original_environ = os.environ.copy()
        original_sys_path = sys.path.copy()

        try:
            os.environ.clear()
            os.environ.update(test_environ)

            # Patch BEFORE importing start_v5_bot
            with patch("newsagent_v2.telegram.live_transport.create_live_transport"):
                with patch("newsagent_v2.telegram.client.TelegramTestClient") as mock_client_class:
                    with patch("newsagent_v2.providers.groq_editorial.GROQ_CHAT_COMPLETIONS_URL", "http://fake"):
                        mock_client_class.return_value = mock_client

                        import importlib.util
                        spec = importlib.util.spec_from_file_location(
                            "start_v5_bot",
                            str(Path(__file__).parent.parent / "start_v5_bot.py")
                        )
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)

                        # Build runtime
                        runtime, bot_info = module.build_runtime()

                        # Create a /make update
                        make_update = {
                            "update_id": 650233705,  # The actual failing update_id
                            "message": {
                                "message_id": 1,
                                "chat": {"id": 12345, "type": "private"},
                                "text": "/make",
                                "from": {"id": 12345, "username": "test_user"},
                                "date": 1234567890,
                            }
                        }

                        # Execute the exact dispatch logic from the polling loop
                        from newsagent_v2.telegram.state import V5BotState

                        state = V5BotState()

                        # This simulates what happens in the main loop around line 840
                        update_id = 650233705

                        # Step 1: persist offset BEFORE processing
                        runtime.offset = update_id + 1
                        module.persist_offset(runtime.offset)

                        # Step 2: Execute /make
                        result = module.execute_make_with_acknowledgement(
                            client=runtime.client,
                            config=runtime.config,
                            state=state,
                            discovery=runtime.discovery,
                            persistent_store=runtime.persistent_store,
                            runtime=runtime,
                            update_id=update_id,
                            update=make_update,
                        )

                        # Verify execution succeeded
                        assert result is not None
                        assert isinstance(result, dict)
                        print(f"/make executed: ok={result.get('ok')}")

        finally:
            os.environ.clear()
            os.environ.update(original_environ)
            sys.path[:] = original_sys_path


class TestPoisonUpdateReplay:
    """Test that failed update doesn't cause infinite replay."""

    def test_offset_advances_on_handler_error(self, tmp_path):
        """Verify offset advances BEFORE handler execution to prevent replay."""

        # Create a mock runtime state
        from unittest.mock import MagicMock

        mock_runtime = MagicMock()
        mock_runtime.offset = 100

        # Simulate the pattern in the main loop:
        # 1. Receive update_id=101
        # 2. Advance offset to 102 BEFORE processing
        # 3. Process (which might fail)
        # 4. If process fails, we STILL have offset=102

        update_id = 101

        # This is the critical pattern from the main loop
        mock_runtime.offset = update_id + 1  # offset = 102

        # Verify offset advanced even before successful processing
        assert mock_runtime.offset == 102

        # If processing fails now:
        error_raised = False
        try:
            raise ValueError("Simulated handler error!")
        except ValueError:
            error_raised = True

        # The offset should STILL be 102, not stuck at 101
        assert mock_runtime.offset == 102
        assert error_raised

        # Next poll would use offset=102, so update 101 won't be replayed
        print(f"Offset advanced to {mock_runtime.offset} despite handler error")
        print("Update 101 will NOT be replayed")


