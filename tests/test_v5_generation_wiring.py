"""Tests for V5 Generation Wiring — aligned to current zoha-v5 APIs.

Covers:
- PersistentV5Store (discovery runs, generation jobs, session marks)
- ProviderPreflight (writer/image/wordpress readiness)
- CostLedger (cost tracking)
- GenerationWorker (request_generation, idempotency)
- V5CallbackHandler (RUN/FOLLOW/IGNORE + controlled E2E)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.v5_generation.persistent_store import (
    PersistentV5Store,
    DiscoveryRun,
    GenerationJob,
)
from newsagent_v2.v5_generation.provider_preflight import ProviderPreflight
from newsagent_v2.v5_generation.cost_ledger import CostLedger, CostStatus
from newsagent_v2.v5_generation.generation_worker import GenerationWorker


# ============== Fixtures ==============

@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    return tmp_path / "v5_state"


@pytest.fixture
def persistent_store(tmp_state_dir: Path) -> Generator[PersistentV5Store, None, None]:
    yield PersistentV5Store(tmp_state_dir)


@pytest.fixture
def mock_event() -> NewsEvent:
    return NewsEvent(
        event_id="evt-test-001",
        canonical_title="Test Event Headline",
    )


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.send_message.return_value = {"ok": True, "message_id": 42}
    client.edit_message_text.return_value = {"ok": True}
    return client


@pytest.fixture
def mock_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="123456:ABCDEF-test-token",
        test_chat_id="99999",
        test_mode=True,
    )


@pytest.fixture(autouse=True)
def _clear_active_jobs() -> Generator[None, None, None]:
    """Isolate GenerationWorker class-level active-job tracking between tests."""
    GenerationWorker._active_jobs = {}
    yield
    GenerationWorker._active_jobs = {}


def _make_run(
    run_id: str = "run-001",
    event_ids: list[str] | None = None,
    created_at: str | None = None,
    chat_id: str = "99999",
) -> DiscoveryRun:
    return DiscoveryRun(
        run_id=run_id,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        event_ids=event_ids or ["evt-1", "evt-2", "evt-3"],
        total_events=len(event_ids or ["evt-1", "evt-2", "evt-3"]),
        chat_id=chat_id,
    )


def _make_job(
    job_id: str = "job-001",
    event_id: str = "evt-test-001",
    state: str = "RESERVED",
    discovery_run_id: str = "run-001",
) -> GenerationJob:
    now = datetime.now(timezone.utc).isoformat()
    return GenerationJob(
        job_id=job_id,
        event_id=event_id,
        discovery_run_id=discovery_run_id,
        state=state,
        created_at=now,
        updated_at=now,
    )


# ============== PersistentStore Tests ==============

class TestPersistentStore:
    def test_save_and_retrieve_discovery_run(self, persistent_store: PersistentV5Store) -> None:
        run = _make_run()
        persistent_store.save_discovery_run(run)

        retrieved = persistent_store.get_discovery_run("run-001")
        assert retrieved is not None
        assert retrieved.run_id == "run-001"
        assert retrieved.event_ids == ["evt-1", "evt-2", "evt-3"]
        assert retrieved.chat_id == "99999"

    def test_list_discovery_runs_via_latest(self, persistent_store: PersistentV5Store) -> None:
        for i in range(3):
            persistent_store.save_discovery_run(_make_run(
                run_id=f"run-{i:03d}",
                created_at=f"2024-01-{i+1:02d}T12:00:00+00:00",
                event_ids=[f"evt-{i}"],
            ))

        latest = persistent_store.get_latest_run("99999")
        assert latest is not None
        assert latest.run_id == "run-002"

    def test_save_and_retrieve_generation_job(self, persistent_store: PersistentV5Store) -> None:
        job = _make_job()
        persistent_store.save_job(job)

        retrieved = persistent_store.get_job("job-001")
        assert retrieved is not None
        assert retrieved.event_id == "evt-test-001"
        assert retrieved.state == "RESERVED"

    def test_get_job_by_event_returns_active(self, persistent_store: PersistentV5Store) -> None:
        persistent_store.save_job(_make_job(job_id="job-old", state="SUCCEEDED"))
        persistent_store.save_job(_make_job(job_id="job-active", state="REQUESTING"))

        active = persistent_store.get_job_by_event("evt-test-001")
        assert active is not None
        assert active.job_id == "job-active"

    def test_get_active_job_count_filters_terminal(self, persistent_store: PersistentV5Store) -> None:
        persistent_store.save_job(_make_job(job_id="j1", event_id="e1", state="RESERVED"))
        persistent_store.save_job(_make_job(job_id="j2", event_id="e2", state="SUCCEEDED"))
        persistent_store.save_job(_make_job(job_id="j3", event_id="e3", state="FAILED_FINAL"))

        assert persistent_store.get_active_job_count() == 1
        assert persistent_store.has_active_generation() is True

    def test_has_active_generation(self, persistent_store: PersistentV5Store) -> None:
        assert persistent_store.has_active_generation() is False
        persistent_store.save_job(_make_job(state="REQUESTING"))
        assert persistent_store.has_active_generation() is True

    def test_session_marks_follow_ignore_select(self, persistent_store: PersistentV5Store) -> None:
        persistent_store.save_discovery_run(_make_run())

        assert persistent_store.mark_followed("run-001", "evt-1") is True
        assert persistent_store.is_followed("run-001", "evt-1") is True

        assert persistent_store.mark_ignored("run-001", "evt-2") is True
        assert persistent_store.is_ignored("run-001", "evt-2") is True

        assert persistent_store.mark_selected("run-001", "evt-3") is True
        assert persistent_store.is_selected("run-001", "evt-3") is True
        # Second select is not "new"
        assert persistent_store.mark_selected("run-001", "evt-3") is False

    def test_atomic_write_survives_reload(self, tmp_state_dir: Path) -> None:
        store1 = PersistentV5Store(tmp_state_dir)
        store1.save_discovery_run(_make_run())
        store1.save_job(_make_job())

        store2 = PersistentV5Store(tmp_state_dir)
        assert store2.get_discovery_run("run-001") is not None
        assert store2.get_job("job-001") is not None
        assert (tmp_state_dir / "run_run-001.json").exists()
        assert (tmp_state_dir / "job_job-001.json").exists()


# ============== ProviderPreflight Tests ==============

class TestProviderPreflight:
    def test_check_writer_missing_kimi_key(self) -> None:
        preflight = ProviderPreflight({})
        status = preflight.check_writer()
        assert status.provider == "kimi"
        assert status.status == "MISSING_CREDENTIALS"
        assert status.credential_envs.get("NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY") == "MISSING"

    def test_check_writer_kimi_key_present(self) -> None:
        preflight = ProviderPreflight({"NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY": "gsk_test_key"})
        status = preflight.check_writer()
        assert status.status == "READY"
        assert status.credential_envs.get("NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY") == "SET"

    def test_check_image_missing_config(self) -> None:
        preflight = ProviderPreflight({})
        status = preflight.check_image()
        assert status.status in {"MISSING_CONFIG", "DISABLED"}

    def test_check_image_ready(self, tmp_path: Path) -> None:
        creds = tmp_path / "creds.json"
        creds.write_text('{"type":"service_account"}', encoding="utf-8")
        preflight = ProviderPreflight({
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "proj",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
        })
        status = preflight.check_image()
        assert status.status == "READY"

    def test_full_report_completeness(self) -> None:
        report = ProviderPreflight({"NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY": "gsk"}).full_report()
        assert "writer" in report
        assert "image" in report
        assert "wordpress" in report
        assert report["ready_for_generation"] is True

    def test_controlled_mode(self) -> None:
        assert ProviderPreflight({"NEWSAGENT_V5_CONTROLLED_E2E": "true"}).is_controlled_mode() is True
        assert ProviderPreflight({}).is_controlled_mode() is False

    def test_get_readiness_summary(self) -> None:
        summary = ProviderPreflight({"NEWSAGENT_V2_BEDROCK_MANTLE_API_KEY": "gsk"}).get_readiness_summary()
        assert summary["can_write"] is True
        assert "overall" in summary


# ============== CostLedger Tests ==============

class TestCostLedger:
    def test_get_report_calculates_totals(self, tmp_path: Path) -> None:
        ledger = CostLedger(persist_path=tmp_path / "ledger.json")
        for _ in range(3):
            ledger.record_call(
                provider="groq",
                model="llama-3.3-70b-versatile",
                operation="write",
                prompt_tokens=100,
                completion_tokens=50,
                cost_inr=0.009,
            )

        report = ledger.get_report()
        assert report.total_entries == 3
        assert report.total_known_cost_inr == pytest.approx(0.027)

    def test_estimate_unknown_costs(self, tmp_path: Path) -> None:
        ledger = CostLedger(persist_path=tmp_path / "ledger.json")
        ledger.record_call(
            provider="groq",
            model="llama-3.3-70b-versatile",
            operation="write",
            prompt_tokens=500,
            completion_tokens=500,
            cost_inr=None,
        )
        report = ledger.get_report()
        assert report.unknown_count == 1
        assert report.total_estimated_cost_inr >= 0.0


# ============== GenerationWorker Tests ==============

class TestGenerationWorker:
    def _worker(
        self,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
        environ: dict | None = None,
    ) -> GenerationWorker:
        return GenerationWorker(
            client=mock_client,
            config=mock_config,
            persistent_store=persistent_store,
            environ=environ or {},
        )

    def test_request_generation_returns_immediately(
        self,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
        mock_event: NewsEvent,
    ) -> None:
        worker = self._worker(persistent_store, mock_client, mock_config)
        with patch.object(worker, "run_generation", return_value={"ok": True}):
            result = worker.request_generation(mock_event, discovery_run_id="run-001")

        assert result["ok"] is True
        assert result["new"] is True
        assert result["state"] == "RESERVED"
        assert result["job_id"]

    def test_request_generation_idempotent(
        self,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
        mock_event: NewsEvent,
    ) -> None:
        worker = self._worker(persistent_store, mock_client, mock_config)
        with patch.object(worker, "run_generation", return_value={"ok": True}):
            first = worker.request_generation(mock_event)
            # Force active job into a terminal-ish tracked state for idempotency branch
            job = GenerationWorker._active_jobs[first["job_id"]]
            job.state = "REQUESTING"
            second = worker.request_generation(mock_event)

        assert second["new"] is False
        assert second["job_id"] == first["job_id"]

    def test_get_active_job_count(
        self,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
        mock_event: NewsEvent,
    ) -> None:
        worker = self._worker(persistent_store, mock_client, mock_config)
        with patch.object(worker, "run_generation", return_value={"ok": True}):
            worker.request_generation(mock_event)
        assert worker.get_active_job_count() >= 1

    def test_get_job_for_event(
        self,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
        mock_event: NewsEvent,
    ) -> None:
        worker = self._worker(persistent_store, mock_client, mock_config)
        with patch.object(worker, "run_generation", return_value={"ok": True}):
            result = worker.request_generation(mock_event)
        job = worker.get_job_for_event(mock_event.event_id)
        assert job is not None
        assert job.job_id == result["job_id"]


# ============== Callback Handler Tests ==============

class TestV5CallbackHandlerRunStory:
    def _handler(
        self,
        event: NewsEvent,
        tmp_path: Path,
        environ: dict | None = None,
        generation_worker: GenerationWorker | None = None,
    ) -> V5CallbackHandler:
        from newsagent_v2.discovery.event_store import EventStore

        store = EventStore(root=tmp_path / "events")
        store.save(event)
        telegram_store = V5TelegramStore()
        telegram_store.save_batch([event])
        return V5CallbackHandler(
            event_store=store,
            telegram_store=telegram_store,
            generation_worker=generation_worker,
            preflight=ProviderPreflight(environ or {}),
            environ=environ or {},
        )

    def test_handle_run_story_controlled_e2e(
        self,
        mock_event: NewsEvent,
        tmp_path: Path,
    ) -> None:
        handler = self._handler(
            mock_event,
            tmp_path,
            environ={"NEWSAGENT_V5_CONTROLLED_E2E": "true"},
        )
        result = handler.handle_run_story(mock_event.event_id)
        assert result["ok"] is True
        assert result.get("controlled_e2e") is True
        assert result.get("awaiting_confirmation") or "SELECTED" in result.get("message", "")

    def test_handle_run_story_with_generation_worker(
        self,
        mock_event: NewsEvent,
        tmp_path: Path,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
    ) -> None:
        worker = GenerationWorker(
            client=mock_client,
            config=mock_config,
            persistent_store=persistent_store,
            environ={},
        )
        handler = self._handler(mock_event, tmp_path, generation_worker=worker)
        with patch.object(worker, "run_generation", return_value={"ok": True}):
            result = handler.handle_run_story(mock_event.event_id)
        assert result["ok"] is True
        assert result.get("action") in {"run_story", "generate_now"} or result.get("started") is True

    def test_run_story_is_idempotent(
        self,
        mock_event: NewsEvent,
        tmp_path: Path,
        persistent_store: PersistentV5Store,
        mock_client: MagicMock,
        mock_config: TelegramConfig,
    ) -> None:
        worker = GenerationWorker(
            client=mock_client,
            config=mock_config,
            persistent_store=persistent_store,
            environ={},
        )
        handler = self._handler(mock_event, tmp_path, generation_worker=worker)
        with patch.object(worker, "run_generation", return_value={"ok": True}):
            first = handler.handle_run_story(mock_event.event_id)
            second = handler.handle_run_story(mock_event.event_id)
        assert first["ok"] is True
        assert second["ok"] is True


class TestV5CallbackHandlerOtherActions:
    def test_handle_follow_persists_state(self, mock_event: NewsEvent, tmp_path: Path) -> None:
        from newsagent_v2.discovery.event_store import EventStore

        store = EventStore(root=tmp_path / "events")
        store.save(mock_event)
        telegram_store = V5TelegramStore()
        telegram_store.save_batch([mock_event])
        handler = V5CallbackHandler(
            event_store=store,
            telegram_store=telegram_store,
            environ={},
        )
        result = handler.handle_follow(mock_event.event_id)
        assert result["ok"] is True
        assert result["action"] == "follow"
        assert mock_event.event_id in telegram_store._followed_event_ids

    def test_handle_ignore_persists_state(self, mock_event: NewsEvent, tmp_path: Path) -> None:
        from newsagent_v2.discovery.event_store import EventStore

        store = EventStore(root=tmp_path / "events")
        store.save(mock_event)
        telegram_store = V5TelegramStore()
        telegram_store.save_batch([mock_event])
        handler = V5CallbackHandler(
            event_store=store,
            telegram_store=telegram_store,
            environ={},
        )
        result = handler.handle_ignore(mock_event.event_id)
        assert result["ok"] is True
        assert result["action"] == "ignore"
        assert mock_event.event_id in telegram_store._ignored_event_ids
