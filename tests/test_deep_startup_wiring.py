"""Deep production startup wiring test.

Exercises EXACT build_runtime() path with external network mocked.
Fixes ALL local constructor/interface mismatches in one pass.
"""

import pytest
import sys
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestDeepProductionStartup:
    """Actually run build_runtime() and catch ALL local wiring errors."""

    @pytest.fixture(scope="function")
    def temp_dirs(self):
        """Create temp directories for test isolation."""
        temp_dir = tempfile.mkdtemp()
        dirs = {
            "temp": Path(temp_dir),
            "state": Path(temp_dir) / "v5_state",
            "versions": Path(temp_dir) / "versions",
            "events": Path(temp_dir) / "events",
            "data": Path(temp_dir) / "data",
            "logs": Path(temp_dir) / "logs",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        yield dirs
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture(scope="function")
    def mock_external_boundaries(self):
        """Mock ONLY external boundaries, not internal NewsAgent classes."""
        mocks = {}

        # Mock Telegram HTTP/network
        mock_transport = Mock()
        mock_transport.get.return_value = {"ok": True, "result": {"username": "test_bot", "id": 12345}}

        # Mock live transport creation
        def mock_create_live_transport(config):
            return mock_transport

        mocks["create_live_transport"] = mock_create_live_transport
        mocks["transport"] = mock_transport

        return mocks

    def test_deep_build_runtime_construction(self, temp_dirs, mock_external_boundaries):
        """Actually execute build_runtime() - catch ALL local wiring errors."""

        # Set up test environment
        test_environ = {
            "NEWSAGENT_V2_TELEGRAM_BOT_TOKEN": "123456789:fake_test_token_for_startup_walk",
            "NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID": "12345",
            "GROQ_API_KEY": "TEST_GROQ_KEY_STARTUP",
            "NEWSAGENT_V5_CONTROLLED_E2E": "true",
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test_project_startup",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "NEWSAGENT_V2_VERTEX_MODEL": "gemini-test",
            "GOOGLE_APPLICATION_CREDENTIALS": str(temp_dirs["data"] / "fake_creds.json"),
        }

        # Create fake Google creds file (Vertex needs this to exist)
        (temp_dirs["data"] / "fake_creds.json").write_text(
            '{"type": "service_account", "project_id": "test", "client_email": "test@test.com"}'
        )

        # Store original
        original_environ = os.environ.copy()
        original_sys_path = sys.path.copy()

        try:
            # Clear and set test environ
            os.environ.clear()
            os.environ.update(test_environ)

            # Make src available
            if str(Path(__file__).parent.parent / "src") not in sys.path:
                sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

            # Create a proper mock client that returns realistic responses
            from newsagent_v2.telegram.client import TelegramTestClient

            mock_client = Mock(spec=TelegramTestClient)
            mock_client.config = Mock()
            mock_client.config.test_chat_id = "12345"

            # _post must return a dict with ok=True for getMe
            def mock_post(method, json_body=None, **kwargs):
                if method == "getMe":
                    return {
                        "ok": True,
                        "status_code": 200,
                        "payload": {
                            "ok": True,
                            "result": {"username": "test_bot", "id": 999999, "first_name": "Test"}
                        }
                    }
                return {"ok": True, "status_code": 200, "payload": {"ok": True}}

            mock_client._post = mock_post
            mock_client.send_message.return_value = {"ok": True, "message_id": 1}
            mock_client.edit_message_text.return_value = {"ok": True}
            mock_client.answer_callback_query.return_value = {"ok": True}

            # Patch ONLY external network boundaries
            with patch("newsagent_v2.telegram.live_transport.create_live_transport") as mock_create_transport:
                with patch("newsagent_v2.telegram.client.TelegramTestClient") as mock_client_class:
                    with patch("newsagent_v2.providers.groq_editorial.GROQ_CHAT_COMPLETIONS_URL", "http://localhost:9999/fake"):

                        mock_create_transport.return_value = mock_external_boundaries["transport"]
                        mock_client_class.return_value = mock_client

                        # NOW import and execute start_v5_bot
                        import importlib.util
                        spec = importlib.util.spec_from_file_location(
                            "start_v5_bot",
                            str(Path(__file__).parent.parent / "start_v5_bot.py")
                        )
                        module = importlib.util.module_from_spec(spec)

                        try:
                            spec.loader.exec_module(module)
                        except Exception as e:
                            pytest.fail(f"Module load failed: {e}")

                        # Now call build_runtime if it exists
                        if not hasattr(module, 'build_runtime'):
                            pytest.fail("build_runtime not found in module")

                        try:
                            runtime, bot_info = module.build_runtime()
                        except TypeError as e:
                            pytest.fail(f"build_runtime TypeError: {e}")
                        except AttributeError as e:
                            pytest.fail(f"build_runtime AttributeError: {e}")
                        except Exception as e:
                            pytest.fail(f"build_runtime failed: {type(e).__name__}: {e}")

                    # Verify runtime object graph constructed
                    assert runtime is not None, "runtime is None"

                    # Check all expected objects exist
                    checks = [
                        ("client", runtime.client),
                        ("config", runtime.config),
                        ("preflight", runtime.preflight),
                        ("persistent_store", runtime.persistent_store),
                        ("version_store", runtime.version_store),
                        ("review_store", runtime.review_store),
                        ("revision_controller", runtime.revision_controller),
                        ("review_handler", runtime.review_handler),
                        ("generation_worker", runtime.generation_worker),
                        ("event_store", runtime.event_store),
                        ("source_registry", runtime.source_registry),
                        ("discovery", runtime.discovery),
                        ("adapter", runtime.adapter),
                    ]

                    for name, obj in checks:
                        assert obj is not None, f"runtime.{name} is None"

                    # Verify GenerationWorker -> CostLedger constructed
                    assert runtime.generation_worker.ledger is not None, "generation_worker.ledger is None"

                    print(f"âœ“ build_runtime() completed")
                    print(f"âœ“ All {len(checks)} runtime objects constructed")
                    print(f"âœ“ GenerationWorker.ledger = {type(runtime.generation_worker.ledger).__name__}")

        finally:
            # Restore
            os.environ.clear()
            os.environ.update(original_environ)
            sys.path[:] = original_sys_path

    def test_controlled_generation_to_provider_boundary(self, temp_dirs):
        """Exercise controlled generation path to provider boundary."""
        from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
        from newsagent_v2.v5_generation.generation_worker import GenerationWorker
        from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight
        from newsagent_v2.discovery.event_clusterer import NewsEvent, EventReport

        # Setup
        handler = V5CallbackHandler(
            event_store=Mock(),
            telegram_store=V5TelegramStore(),
            environ={
                "NEWSAGENT_V5_CONTROLLED_E2E": "true",
                "GROQ_API_KEY": "test",
                "NEWSAGENT_V2_VERTEX_ENABLED": "true",
                "NEWSAGENT_V2_VERTEX_PROJECT": "test",
                "NEWSAGENT_V2_VERTEX_LOCATION": "test",
                "GOOGLE_APPLICATION_CREDENTIALS": "/fake",
            },
        )

        # Mock generation worker
        mock_worker = Mock(spec=GenerationWorker)
        mock_worker.get_active_job_count.return_value = 0
        mock_worker.get_job_for_event.return_value = None
        mock_worker.request_generation.return_value = {
            "ok": True, "job_id": "job-001", "state": "RESERVED", "new": True
        }
        handler.generation_worker = mock_worker

        # Mock preflight
        mock_preflight = Mock(spec=ProviderPreflight)
        writer_status = Mock()
        writer_status.status = "READY"
        image_status = Mock()
        image_status.status = "READY"
        mock_preflight.check_writer.return_value = writer_status
        mock_preflight.check_image.return_value = image_status
        handler.preflight = mock_preflight

        # Create event
        event = NewsEvent(
            event_id="test-evt-001",
            canonical_title="Test Event",
            topic="technology",
            entities=frozenset(["test"]),
        )
        event.reports.append(EventReport(
            report_id="rpt-001",
            source="test",
            source_id="src-001",
            source_authority=0.8,
            headline="Test",
            url="http://test",
            published_at="2024-01-01T00:00:00Z",
            retrieved_at="2024-01-01T00:00:00Z",
            description="Test",
            entities=["test"],
            raw_item_id="raw-001",
        ))

        handler.event_store.get.return_value = event

        # STEP 1: RUN STORY
        result1 = handler.handle_run_story(event.event_id)
        assert result1["ok"] is True
        assert result1["awaiting_confirmation"] is True
        assert "reply_markup" in result1

        # STEP 2: GENERATE NOW
        result2 = handler.handle_generate_now(event.event_id)
        assert result2["ok"] is True
        assert result2["started"] is True

        # Verify reached provider boundary (request_generation called)
        mock_worker.request_generation.assert_called_once()

        print(f"âœ“ Controlled generation path reaches provider boundary")

    def test_review_callback_routing(self):
        """Verify review callback routing constructs without errors."""
        from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
        from newsagent_v2.v5_generation.revision_controller import RevisionController
        from newsagent_v2.v5_generation.version_store import VersionStore
        from newsagent_v2.v5_generation.persistent_review import PersistentReviewStore

        # Construct review handler (this validates all dependencies)
        with tempfile.TemporaryDirectory() as tmpdir:
            review_store = PersistentReviewStore(Path(tmpdir))
            version_store = VersionStore(Path(tmpdir) / "versions")
            revision_controller = RevisionController(
                version_store=version_store,
                environ={},
            )

            handler = V5ReviewCallbackHandler(
                review_store=review_store,
                revision_controller=revision_controller,
                version_store=version_store,
                persistent_store=Mock(),
            )

            # Test callback parsing for review actions
            assert handler.parse_callback("rate_article:evt-001:v1") is not None
            assert handler.parse_callback("feedback_article:evt-001:v1") is not None
            assert handler.parse_callback("revise:evt-001:v1") is not None
            assert handler.parse_callback("approve:evt-001") is not None

            print(f"âœ“ Review callback routing constructs")

    def test_revision_routing(self):
        """Verify revision path constructs without errors."""
        from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
        from newsagent_v2.v5_generation.revision_controller import RevisionController
        from newsagent_v2.v5_generation.version_store import VersionStore
        from newsagent_v2.v5_generation.persistent_review import PersistentReviewStore

        with tempfile.TemporaryDirectory() as tmpdir:
            review_store = PersistentReviewStore(Path(tmpdir))
            version_store = VersionStore(Path(tmpdir) / "versions")
            revision_controller = RevisionController(
                version_store=version_store,
                environ={},
            )

            handler = V5ReviewCallbackHandler(
                review_store=review_store,
                revision_controller=revision_controller,
                version_store=version_store,
                persistent_store=Mock(),
            )

            # Verify revision controller is wired
            assert handler.revision_controller is not None

            print(f"âœ“ Revision routing constructs")

    def test_approval_routing(self):
        """Verify approval path constructs without errors."""
        from newsagent_v2.telegram.v5_review_callbacks import V5ReviewCallbackHandler
        from newsagent_v2.v5_generation.revision_controller import RevisionController
        from newsagent_v2.v5_generation.version_store import VersionStore
        from newsagent_v2.v5_generation.persistent_review import PersistentReviewStore

        with tempfile.TemporaryDirectory() as tmpdir:
            review_store = PersistentReviewStore(Path(tmpdir))
            version_store = VersionStore(Path(tmpdir) / "versions")
            revision_controller = RevisionController(
                version_store=version_store,
                environ={},
            )

            handler = V5ReviewCallbackHandler(
                review_store=review_store,
                revision_controller=revision_controller,
                version_store=version_store,
                persistent_store=Mock(),
            )

            # Test approval parsing
            parsed = handler.parse_callback("approve:evt-001")
            assert parsed is not None
            assert parsed["action"] == "approve"

            print(f"âœ“ Approval routing constructs")


def test_generation_worker_cost_ledger_construction():
    """Direct test of GenerationWorker -> CostLedger construction."""
    from newsagent_v2.v5_generation.generation_worker import GenerationWorker
    from newsagent_v2.v5_generation.persistent_store import PersistentV5Store
    from newsagent_v2.telegram.client import TelegramTestClient
    from newsagent_v2.telegram.config import TelegramConfig

    with tempfile.TemporaryDirectory() as tmpdir:
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

        assert worker.ledger is not None
        print(f"âœ“ GenerationWorker -> CostLedger constructs: {type(worker.ledger).__name__}")


