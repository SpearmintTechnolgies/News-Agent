"""Deep runtime construction smoke test.

Instantiates build_runtime() with Telegram/network mocked.
Verifies the V5 object graph (GenerationWorker, PersistentV5Store, etc.).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_build_runtime_deep_construction(tmp_path: Path) -> None:
    """build_runtime() must construct the full object graph without live network."""
    state_dir = tmp_path / "state"
    version_dir = tmp_path / "versions"
    events_dir = tmp_path / "events"
    data_dir = tmp_path / "data"
    for d in (state_dir, version_dir, events_dir, data_dir):
        d.mkdir(parents=True, exist_ok=True)

    (data_dir / "fake_creds.json").write_text(
        '{"type": "service_account", "project_id": "test"}',
        encoding="utf-8",
    )

    test_environ = {
        "NEWSAGENT_V2_TELEGRAM_BOT_TOKEN": "123456:ABCDEF-fake-token-for-testing",
        "NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID": "12345",
        "GROQ_API_KEY": "TEST_GROQ_KEY",
        "NEWSAGENT_V5_CONTROLLED_E2E": "true",
        "NEWSAGENT_V2_VERTEX_ENABLED": "true",
        "NEWSAGENT_V2_VERTEX_PROJECT": "test_project",
        "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
        "NEWSAGENT_V2_VERTEX_MODEL": "gemini-1.0",
        "GOOGLE_APPLICATION_CREDENTIALS": str(data_dir / "fake_creds.json"),
    }

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "start_v5_bot",
        str(Path(__file__).parent.parent / "start_v5_bot.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    original_environ = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(test_environ)

        mock_client = MagicMock()
        mock_client._post.return_value = {
            "ok": True,
            "payload": {"result": {"username": "test_bot", "id": 12345}},
        }
        mock_client.send_message.return_value = {"ok": True, "message_id": 1}

        with (
            patch("newsagent_v2.telegram.live_transport.create_live_transport", return_value=MagicMock()),
            patch("newsagent_v2.telegram.client.TelegramTestClient", return_value=mock_client),
            patch("newsagent_v2.telegram.singleton.acquire_singleton_lock", return_value=True),
            patch("newsagent_v2.telegram.singleton.release_singleton_lock"),
            patch("newsagent_v2.v5_generation.version_store.VersionStore") as mock_vs,
            patch("newsagent_v2.approval.store.ApprovalStore") as mock_as,
            patch("newsagent_v2.discovery.event_store.EventStore") as mock_es,
        ):
            mock_vs.return_value = MagicMock()
            mock_as.return_value = MagicMock()
            mock_es.return_value = MagicMock()

            spec.loader.exec_module(module)
            module.V5_STATE_DIR = state_dir

            assert hasattr(module, "build_runtime")
            runtime, bot_info = module.build_runtime()

            assert runtime is not None
            assert bot_info.get("username") == "test_bot"
            assert runtime.generation_worker is not None
            assert runtime.persistent_store is not None
            assert runtime.preflight is not None
            assert runtime.generation_worker.ledger is not None
            assert runtime._controlled_e2e is True
    finally:
        os.environ.clear()
        os.environ.update(original_environ)
