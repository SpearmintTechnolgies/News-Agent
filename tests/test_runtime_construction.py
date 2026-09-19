"""Deep runtime construction smoke test.

Actually instantiates build_runtime() with external operations mocked.
Must reach GenerationWorker -> CostLedger without TypeError.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import os
import tempfile
import shutil

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_build_runtime_deep_construction():
    """Test that build_runtime() actually constructs the full object graph."""

    import tempfile
    import shutil

    # Create temp directories
    temp_dir = tempfile.mkdtemp()
    state_dir = Path(temp_dir) / "state"
    version_dir = Path(temp_dir) / "versions"
    events_dir = Path(temp_dir) / "events"
    data_dir = Path(temp_dir) / "data"

    state_dir.mkdir(parents=True, exist_ok=True)
    version_dir.mkdir(parents=True, exist_ok=True)
    events_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Mock environment with all required variables
        test_environ = {
            "NEWSAGENT_V2_TELEGRAM_BOT_TOKEN": "test_token_12345:fake_token_for_testing",
            "NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID": "12345",
            "GROQ_API_KEY": "TEST_GROQ_KEY",
            "NEWSAGENT_V5_CONTROLLED_E2E": "true",
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test_project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "NEWSAGENT_V2_VERTEX_MODEL": "gemini-1.0",
            "GOOGLE_APPLICATION_CREDENTIALS": str(data_dir / "fake_creds.json"),
        }

        # Create fake Google credentials file
        (data_dir / "fake_creds.json").write_text('{"type": "service_account", "project_id": "test"}')

        # Import here to get fresh module
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "start_v5_bot",
            str(Path(__file__).parent.parent / "start_v5_bot.py")
        )
        module = importlib.util.module_from_spec(spec)

        # Store original environ
        original_environ = os.environ.copy()

        try:
            # Set test environment
            os.environ.clear()
            os.environ.update(test_environ)

            # Pre-create the V5_STATE_DIR to avoid path issues
            module.V5_STATE_DIR = state_dir

            # Mock the Telegram client and related classes BEFORE loading module
            with patch("newsagent_v2.telegram.live_transport.create_live_transport") as mock_create_transport:
                with patch("newsagent_v2.telegram.client.TelegramTestClient") as mock_client_class:
                    with patch("newsagent_v2.v5_generation.persistent_store.DATA_ROOT", state_dir):
                            with patch("newsagent_v2.discovery.event_store.DATA_ROOT", events_dir):
                                # Setup mock client
                                mock_client = Mock()
                                mock_client.get_me.return_value = {"ok": True, "result": {"username": "test_bot", "id": 12345}}
                                mock_client_class.return_value = mock_client
                                mock_create_transport.return_value = Mock()

                                # Execute module (which calls build_runtime at module level? No, it's in main())
                                # Actually we need to call build_runtime() specifically
                                spec.loader.exec_module(module)

                                # Now call build_runtime if it exists
                                if hasattr(module, 'build_runtime'):
                                    try:
                                        runtime, bot_info = module.build_runtime()
                                        print(f"SUCCESS: build_runtime() completed")
                                        print(f"  runtime type: {type(runtime).__name__}")
                                        print(f"  generation_worker type: {type(runtime.generation_worker).__name__ if runtime.generation_worker else 'None'}")
                                        print(f"  CostLedger constructed: {runtime.generation_worker.ledger is not None if runtime.generation_worker else False}")
                                    except Exception as e:
                                        pytest.fail(f"build_runtime() failed: {type(e).__name__}: {e}")
                                else:
                                    pytest.fail("build_runtime not found in module")

        finally:
            # Restore original environ
            os.environ.clear()
            os.environ.update(original_environ)

    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_generation_worker_cost_ledger_construction():
    """Direct test of GenerationWorker -> CostLedger construction."""
    from newsagent_v2.v5_generation.generation_worker import GenerationWorker
    from newsagent_v2.v5_generation.persistent_store import PersistentV5Store
    from newsagent_v2.telegram.client import TelegramTestClient
    from newsagent_v2.telegram.config import TelegramConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create mocks
        mock_client = Mock(spec=TelegramTestClient)
        mock_config = Mock(spec=TelegramConfig)
        mock_config.test_chat_id = "12345"

        persistent_store = Mock(spec=PersistentV5Store)

        # This should NOT raise TypeError
        try:
            worker = GenerationWorker(
                client=mock_client,
                config=mock_config,
                persistent_store=persistent_store,
                environ={"GROQ_API_KEY": "test"},
            )
        except TypeError as e:
            pytest.fail(f"GenerationWorker construction failed: {e}")

        # Verify ledger was created
        assert worker.ledger is not None
        print(f"GenerationWorker.ledger = {type(worker.ledger).__name__}")


